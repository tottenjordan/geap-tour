"""Online quality monitor — continuously score live coordinator traffic.

The native Vertex Online Evaluators returned ``INSUFFICIENT_DATA`` under a default
deploy because prompt/response content never landed on the ``call_llm`` spans the
evaluator parses — **not** a hard platform strip: the managed ``AdkApp``
``set_up()`` forces the ADK span-content gate closed unless deployed with
``AdkApp(enable_tracing=True)`` (wired behind the opt-in
``ENABLE_SPAN_CONTENT_CAPTURE`` flag; see
``docs/notes/online-eval-content-capture.md`` and memory
``online-eval-content-capture-blocked``). This client-side monitor is the shipped
default **by choice** — model-neutral, no privacy-off content capture on the
served engine — while the native path stays unblockable on demand. The live
response content is available client-side off ``stream_query`` (the traffic
generator already captures ``full_response``). This module samples that live
traffic, scores each ``(prompt, response)`` with the SAME rubrics the offline
bridge uses (the delegation-aware ``geap_tool_use`` judge + the
``policy_compliance`` judge) plus a helpfulness rubric, and publishes a continuous
``agent_online_eval/*`` series (``eval_mode=online``) onto the same dashboard +
alert surface as the offline snapshot.

Two honest surfaces, never blurred:

* ``agent_eval/*``        — periodic offline snapshot (``publish_offline_eval``,
  ``eval_mode=offline``), one write per eval run.
* ``agent_online_eval/*`` — continuous online sampled scores (this module,
  ``eval_mode=online``), on the SAME 1-5 axis so both chart together and share
  the 3.0 alert floor.

The pure core (prompt builders, per-interaction scoring, aggregation, sampling,
publish) is side-effect-free and unit-tested with an injected ``generate_fn`` and
a fake metric client — no GCP, no live engine. The CLI drives ``stream_query``
for a live demo, or scores a JSON of externally-captured ``(prompt, response)``
pairs (``--from-json``, e.g. sampled from a real traffic run).
"""

from __future__ import annotations

import argparse
import json
import re
from typing import TYPE_CHECKING

from src.eval.policy_judge import build_policy_prompt
from src.eval.quality_alerts import ONLINE_MONITORED_METRICS
from src.eval.tool_use_judge import build_tool_use_prompt

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from src.observability.metrics import MetricsWriter

DEFAULT_JUDGE_MODEL = "gemini-2.5-flash"

# Bare metric names published on the online surface (from the single source of
# truth so there's no drift with the alert policies / dashboard).
ONLINE_MONITORED_METRIC_NAMES = [name for name, _threshold in ONLINE_MONITORED_METRICS]

_SCORE_RE = re.compile(r"score\s*:?\s*\**\s*([1-5])", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Pure scoring core
# --------------------------------------------------------------------------- #
def parse_score(text: str | None) -> float | None:
    """Extract the judge's final ``Score: N`` (1-5) and map it to 0-1 (``N/5``).

    Returns ``None`` when no score is present so unparseable verdicts are dropped
    (not counted as zero). Uses the *last* match — judges often restate criterion
    scores before the final verdict. Mirrors the parsers in the standalone judges.
    """
    if not text:
        return None
    matches = _SCORE_RE.findall(str(text))
    if not matches:
        return None
    return int(matches[-1]) / 5.0


def build_helpfulness_prompt(prompt: str, response: str) -> str:
    """Render a concise 1-5 helpfulness rubric for one ``(prompt, response)``.

    Helpfulness is the online-only rubric: the offline snapshot's helpfulness
    comes from the SDK ``FINAL_RESPONSE_QUALITY`` metric, which can't be called
    per-interaction, so we score it directly with the same ``Score: <1-5>``
    contract the ``parse_score`` parser expects.
    """
    return (
        "You are a strict evaluator scoring an AI travel & expense assistant's reply.\n"
        "Rate how HELPFUL the response is to the user's request on a 1-5 scale:\n"
        "  5 = fully addresses the request: correct, complete, actionable.\n"
        "  4 = addresses the request with only minor gaps.\n"
        "  3 = partially helpful; missing or unclear on key points.\n"
        "  2 = largely unhelpful or off-target.\n"
        "  1 = does not address the request at all.\n"
        "Judge only helpfulness (not tone or policy).\n\n"
        f"User request:\n{prompt}\n\n"
        f"Assistant response:\n{response}\n\n"
        "End your answer with a single line exactly: Score: <1-5>"
    )


# metric_name -> rubric prompt builder. ``tool_use_accuracy`` and
# ``policy_compliance`` reuse the EXACT standalone-judge rubrics (no drift with
# the offline bridge); ``helpfulness`` is the online-only rubric above.
RUBRIC_BUILDERS: dict[str, Callable[[str, str], str]] = {
    "helpfulness": build_helpfulness_prompt,
    "tool_use_accuracy": build_tool_use_prompt,
    "policy_compliance": build_policy_prompt,
}


def score_interaction(
    prompt: str,
    response: str,
    generate_fn: Callable[[str], str],
    metrics: Sequence[str] | None = None,
) -> dict[str, float]:
    """Score one ``(prompt, response)`` with each rubric; skip unparseable verdicts.

    ``generate_fn`` takes a rendered judge prompt and returns the judge's raw
    text. Returns ``{metric_name: 0-1}`` for every metric that produced a
    parseable score (a metric whose verdict didn't parse is omitted, not zeroed).
    ``metrics`` restricts which rubrics run (defaults to all).
    """
    names = list(metrics) if metrics is not None else list(RUBRIC_BUILDERS)
    out: dict[str, float] = {}
    for name in names:
        raw = generate_fn(RUBRIC_BUILDERS[name](prompt, response))
        score = parse_score(raw)
        if score is not None:
            out[name] = score
    return out


def sample_interactions(interactions: Sequence, sample_rate: float) -> list:
    """Deterministically pick a fraction of interactions to score.

    Scoring every request with an LLM judge is expensive, so a monitor samples.
    Uses a fixed stride (every ``round(1/rate)``-th item) rather than RNG so the
    selection is reproducible and testable. ``sample_rate >= 1`` scores all;
    ``sample_rate <= 0`` scores none.
    """
    items = list(interactions)
    if sample_rate >= 1:
        return items
    if sample_rate <= 0:
        return []
    stride = max(1, round(1 / sample_rate))
    return items[::stride]


def aggregate_scores(interaction_scores: Sequence[Mapping[str, float]]) -> dict:
    """Mean each metric over the interactions that produced it.

    Returns ``{"scores": {name: mean 0-1}, "counts": {name: n},
    "n_interactions": N}``. A metric no interaction scored is absent from
    ``scores`` (so downstream publish emits nothing spurious for it).
    """
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for scores in interaction_scores:
        for name, value in scores.items():
            sums[name] = sums.get(name, 0.0) + value
            counts[name] = counts.get(name, 0) + 1
    means = {name: sums[name] / counts[name] for name in sums}
    return {
        "scores": means,
        "counts": counts,
        "n_interactions": len(interaction_scores),
    }


# --------------------------------------------------------------------------- #
# Publish
# --------------------------------------------------------------------------- #
def _to_monitored_scale(score: float) -> float:
    """Map a 0-1 score onto the 1-5 monitored axis (``0.6 -> 3.0``)."""
    return round(float(score) * 5.0, 3)


def publish_online_scores(
    scores: Mapping[str, float],
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
) -> dict[str, float]:
    """Publish aggregated online quality scores to ``agent_online_eval/*``.

    ``scores`` is ``{metric_name: 0-1}`` (typically :func:`aggregate_scores`'
    ``"scores"``). Values are scaled 0-1 -> 1-5, filtered to
    ``ONLINE_MONITORED_METRICS`` (non-monitored keys dropped — no metric drift),
    tagged ``eval_mode=online``, and written. Returns the exact ``{name: value}``
    emitted (empty dict when nothing monitored was present — writes nothing).
    """
    from src.observability.metrics import write_online_quality_scores

    monitored = set(ONLINE_MONITORED_METRIC_NAMES)
    published = {
        name: _to_monitored_scale(value) for name, value in scores.items() if name in monitored
    }
    if not published:
        return {}
    labels = {"eval_mode": "online", **(extra_labels or {})}
    write_online_quality_scores(published, writer=writer, extra_labels=labels)
    return published


def score_and_publish(
    pairs: Sequence[tuple[str, str]],
    *,
    generate_fn: Callable[[str], str],
    sample_rate: float = 1.0,
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Sample → score → aggregate → publish a batch of captured interactions.

    The shared core of both CLI paths (live ``stream_query`` and ``--from-json``).
    Returns ``{"aggregate", "published", "n_captured", "n_sampled"}``. When
    ``dry_run`` is set, scores are still computed and returned but nothing is
    written to Cloud Monitoring.
    """
    sampled = sample_interactions(pairs, sample_rate)
    per_interaction = [score_interaction(p, r, generate_fn) for p, r in sampled]
    agg = aggregate_scores(per_interaction)
    published: dict[str, float] = {}
    if not dry_run:
        published = publish_online_scores(agg["scores"], writer=writer, extra_labels=extra_labels)
    return {
        "aggregate": agg,
        "published": published,
        "n_captured": len(pairs),
        "n_sampled": len(sampled),
    }


# --------------------------------------------------------------------------- #
# Live capture (thin wrappers over the deployed engine — not unit-tested)
# --------------------------------------------------------------------------- #
# A small cross-domain probe set (travel, expense, routing) so a live demo run
# exercises all three rubrics. In production the (prompt, response) pairs would
# be sampled from real captured traffic (--from-json); this is the self-driven
# demo default.
ONLINE_PROBE_PROMPTS = [
    "Find me flights from SFO to JFK on June 15th",
    "Search for hotels in New York under $350 per night",
    "Check if a $50 meal expense is within policy",
    "Submit a $45 meals expense for lunch meeting, user ID EMP001",
    "I need to book a trip to Chicago and submit my last meal receipt",
    "Show all expenses for user EMP001",
]


def _default_generate_fn(
    judge_model: str, project: str | None, location: str | None
) -> Callable[[str], str]:
    """Build a direct google.genai judge call (Vertex backend) — same as the judges."""
    from google import genai

    from src.config import GCP_PROJECT_ID, GCP_REGION

    client = genai.Client(
        vertexai=True,
        project=project or GCP_PROJECT_ID,
        location=location or GCP_REGION,
    )

    def _generate(prompt: str) -> str:
        resp = client.models.generate_content(model=judge_model, contents=prompt)
        return resp.text or ""

    return _generate


def capture_live_interactions(
    agent, prompts: Sequence[str], user_id: str = "online-monitor-user"
) -> list[tuple[str, str]]:
    """Drive each prompt through the deployed agent, capturing ``(prompt, response)``.

    Uses ``stream_query`` and reuses the traffic generator's ``_extract_text`` so
    the captured text matches what the traffic tooling records. This is the
    load-bearing lever: the response CONTENT is available client-side even though
    the managed runtime strips it from the trace surface.
    """
    from src.traffic.generate_traffic import _extract_text

    pairs: list[tuple[str, str]] = []
    for prompt in prompts:
        session = agent.create_session(user_id=user_id)
        response = agent.stream_query(user_id=user_id, session_id=session["id"], message=prompt)
        text = "".join(_extract_text(chunk) for chunk in response)
        pairs.append((prompt, text))
    return pairs


def run_online_monitor(
    agent_id: str | None = None,
    *,
    n_interactions: int | None = None,
    sample_rate: float = 1.0,
    prompts: Sequence[str] | None = None,
    judge_model: str = DEFAULT_JUDGE_MODEL,
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
    dry_run: bool = False,
    agent=None,
    generate_fn: Callable[[str], str] | None = None,
) -> dict:
    """Sample live coordinator traffic, score it, and publish ``agent_online_eval/*``.

    Drives the probe prompts (or ``prompts``, capped at ``n_interactions``)
    against the deployed engine, captures responses client-side, samples, scores
    with the judge model, and publishes. ``agent``/``generate_fn``/``writer`` are
    injectable so the pipeline is testable; the CLI wires the real deployed engine
    and the google.genai judge.
    """
    from src.config import AGENT_ENGINE_ID, GCP_PROJECT_ID, GCP_REGION

    probe = list(prompts) if prompts is not None else list(ONLINE_PROBE_PROMPTS)
    if n_interactions is not None:
        probe = probe[:n_interactions]

    if agent is None:
        import vertexai
        from vertexai import agent_engines

        from src.eval.batch_eval import _resolve_agent_resource_name

        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        agent = agent_engines.get(_resolve_agent_resource_name(agent_id or AGENT_ENGINE_ID))

    pairs = capture_live_interactions(agent, probe)
    gen = generate_fn or _default_generate_fn(judge_model, None, None)
    return score_and_publish(
        pairs,
        generate_fn=gen,
        sample_rate=sample_rate,
        writer=writer,
        extra_labels=extra_labels,
        dry_run=dry_run,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _load_pairs(path: str) -> list[tuple[str, str]]:
    """Load externally-captured interactions → ``[(prompt, response), ...]``.

    Accepts a JSON list of ``{"prompt": ..., "response": ...}`` objects or of
    ``[prompt, response]`` 2-tuples.
    """
    with open(path) as f:
        data = json.load(f)
    pairs: list[tuple[str, str]] = []
    for item in data:
        if isinstance(item, dict):
            pairs.append((item.get("prompt", ""), item.get("response", "")))
        else:
            pairs.append((item[0], item[1]))
    return pairs


def _print_summary(result: dict, *, dry_run: bool) -> None:
    agg = result["aggregate"]
    print(
        f"\nOnline quality monitor: scored {result['n_sampled']}/{result['n_captured']} "
        f"captured interactions ({agg['n_interactions']} judged)"
    )
    for name in ONLINE_MONITORED_METRIC_NAMES:
        mean = agg["scores"].get(name)
        count = agg["counts"].get(name, 0)
        if mean is None:
            print(f"  {name}: n/a (0 scored)")
        else:
            print(f"  {name}: {mean:.3f} (0-1) → {_to_monitored_scale(mean)} (1-5) over {count}")
    prefix = "[dry-run] would publish" if dry_run else "published"
    print(f"{prefix}: {json.dumps(result['published'], indent=2, sort_keys=True)}")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the online quality monitor."""
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--agent-id",
        default=None,
        help="engine (bare id or full resource name) to sample live via stream_query "
        "(defaults to the AGENT_ENGINE_ID env)",
    )
    source.add_argument(
        "--from-json",
        metavar="PATH",
        help="score externally-captured [{prompt,response},...] pairs instead of live traffic",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=None,
        help="cap the number of live probe prompts driven (default: all probe prompts)",
    )
    parser.add_argument(
        "--sample-rate",
        type=float,
        default=1.0,
        help="fraction of captured interactions to score (default: 1.0 = all)",
    )
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL, help="judge model id")
    parser.add_argument(
        "--label",
        action="append",
        metavar="KEY=VALUE",
        help="extra label stamped on every published series (repeatable; e.g. --label model=…)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="score and print without writing to Cloud Monitoring",
    )
    args = parser.parse_args(argv)

    from src.observability.metrics import parse_labels

    extra_labels = parse_labels(args.label)

    if args.from_json:
        pairs = _load_pairs(args.from_json)
        result = score_and_publish(
            pairs,
            generate_fn=_default_generate_fn(args.judge_model, None, None),
            sample_rate=args.sample_rate,
            extra_labels=extra_labels,
            dry_run=args.dry_run,
        )
    else:
        result = run_online_monitor(
            agent_id=args.agent_id,
            n_interactions=args.samples,
            sample_rate=args.sample_rate,
            judge_model=args.judge_model,
            extra_labels=extra_labels,
            dry_run=args.dry_run,
        )

    _print_summary(result, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
