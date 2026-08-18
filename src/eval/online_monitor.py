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

from src.eval import raw_stream
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


def score_interaction_panel(
    prompt: str,
    response: str,
    judges: Sequence[Callable[[str], str]],
    metrics: Sequence[str] | None = None,
) -> tuple[dict[str, float], dict[str, list[float | None]]]:
    """Score one ``(prompt, response)`` with a DIVERSE JUDGE PANEL per rubric.

    Like :func:`score_interaction`, but each rubric is scored by *every* judge in
    ``judges`` and aggregated with the robust **median** (one contrarian judge
    cannot swing the aggregate the way a mean would) — the same cross-generation
    Gemini panel the offline judges use (:mod:`src.eval.judge_panel`), so the online surface is
    no longer a single autorater's unchecked verdict. Returns ``(medians,
    per_judge)``: ``medians`` is ``{name: 0-1}`` for every rubric whose panel
    produced at least one parseable score (the median is dropped, not zeroed, when
    all judges failed), and ``per_judge`` is ``{name: [score|None, ...]}`` (one
    entry per judge, in panel order) so downstream can compute inter-rater
    reliability (:func:`src.eval.judge_panel.panel_reliability`).
    """
    from src.eval.judge_panel import score_with_panel

    names = list(metrics) if metrics is not None else list(RUBRIC_BUILDERS)
    medians: dict[str, float] = {}
    per_judge: dict[str, list[float | None]] = {}
    for name in names:
        result = score_with_panel(RUBRIC_BUILDERS[name](prompt, response), judges, parse_score)
        per_judge[name] = result["per_judge"]
        if result["median"] is not None:
            medians[name] = result["median"]
    return medians, per_judge


def is_infra_empty(response: str) -> bool:
    """True for an empty / error-shaped response — an infra failure, not low quality.

    The managed runtime can return an empty body on an HTTP 200 (cold-start or
    high-complexity timeout), and the inference harness emits an ``{"error": ...}``
    shape it couldn't parse. Judging these as helpfulness ≈ low silently drags the
    online quality mean and trips the quality alert — a *quality* alarm for an
    *infra* problem (see memory ``online-helpfulness-dips-are-empty-streams``). We
    detect them up front and account for them on a separate infra signal instead.
    Mirrors ``policy_judge._is_error_response``.
    """
    s = str(response).strip()
    return (not s) or s.startswith('{"error"')


def partition_interactions(
    pairs: Sequence[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Split ``(prompt, response)`` pairs into ``(real, infra_empty)``.

    Only ``real`` responses go to the quality judges; ``infra_empty`` are counted
    toward :data:`infra_empty_rate` so an empty-at-200 stream is visible as an
    infra failure rather than masquerading as a low quality score.
    """
    real: list[tuple[str, str]] = []
    empty: list[tuple[str, str]] = []
    for prompt, response in pairs:
        (empty if is_infra_empty(response) else real).append((prompt, response))
    return real, empty


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
    """Mean each metric over the interactions that produced it, with uncertainty.

    Returns ``{"scores": {name: mean 0-1}, "counts": {name: n},
    "ci": {name: (lo, hi)}, "low_confidence": {name: bool},
    "n_interactions": N}``. ``ci`` is a percentile-bootstrap CI on each mean and
    ``low_confidence`` flags metrics scored over fewer than the sample floor
    (:data:`src.eval.stats.MIN_SAMPLES`) — so a mean over 3 interactions is not
    read with the same trust as one over 300. A metric no interaction scored is
    absent from ``scores`` (so downstream publish emits nothing spurious for it).
    """
    from src.eval import stats

    values: dict[str, list[float]] = {}
    for scores in interaction_scores:
        for name, value in scores.items():
            values.setdefault(name, []).append(value)
    means = {name: sum(v) / len(v) for name, v in values.items()}
    counts = {name: len(v) for name, v in values.items()}
    ci = {name: stats.bootstrap_mean_ci(v) for name, v in values.items()}
    low_confidence = {name: stats.is_low_confidence(n) for name, n in counts.items()}
    return {
        "scores": means,
        "counts": counts,
        "ci": ci,
        "low_confidence": low_confidence,
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


def publish_infra_empty_rate(
    rate: float,
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
) -> dict[str, float]:
    """Publish the ``infra_empty_rate`` (0-1) to ``agent_online_eval/*`` verbatim.

    A separate infra signal from the quality rubrics — alerts on the CEILING
    (``GT``, see ``quality_alerts.ONLINE_INFRA_METRICS``), so a rising empty-at-200
    rate pages as an infra problem instead of dragging the quality mean.
    """
    from src.observability.metrics import write_online_infra_metrics

    scores = {"infra_empty_rate": round(float(rate), 4)}
    labels = {"eval_mode": "online", **(extra_labels or {})}
    write_online_infra_metrics(scores, writer=writer, extra_labels=labels)
    return scores


def score_and_publish(
    pairs: Sequence[tuple[str, str]],
    *,
    generate_fn: Callable[[str], str] | None = None,
    judges: Sequence[Callable[[str], str]] | None = None,
    sample_rate: float = 1.0,
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Sample → partition → score → aggregate → publish a batch of interactions.

    The shared core of both CLI paths (live ``stream_query`` and ``--from-json``).
    Infra-empty responses (empty-at-200 / error-shaped) are separated *before*
    judging so they never drag the quality mean; they are counted toward
    ``infra_empty_rate`` and published as a distinct infra signal.

    Scoring uses either a **single** judge (``generate_fn``) or a **diverse
    panel** (``judges`` — the cross-generation median-of-panel scorer, see
    :func:`score_interaction_panel`); pass exactly one. In panel mode the returned
    ``aggregate`` carries an extra ``"reliability"`` block (per-rubric Krippendorff
    alpha + mean disagreement) so a low-agreement panel is visible rather than
    silently averaged. Returns ``{"aggregate", "published", "infra_published",
    "n_captured", "n_sampled", "n_infra_empty", "infra_empty_rate"}``. When
    ``dry_run`` is set, scores are still computed and returned but nothing is
    written to Cloud Monitoring.
    """
    if generate_fn is None and judges is None:
        raise ValueError("score_and_publish requires either generate_fn or judges")
    sampled = sample_interactions(pairs, sample_rate)
    real, empty = partition_interactions(sampled)
    if judges is not None:
        from src.eval.judge_panel import panel_reliability

        per_interaction: list[dict[str, float]] = []
        per_judge_rows: dict[str, list[list[float | None]]] = {n: [] for n in RUBRIC_BUILDERS}
        for p, r in real:
            medians, per_judge = score_interaction_panel(p, r, judges)
            per_interaction.append(medians)
            for name, row in per_judge.items():
                per_judge_rows[name].append(row)
        agg = aggregate_scores(per_interaction)
        agg["reliability"] = {n: panel_reliability(rows) for n, rows in per_judge_rows.items()}
    else:
        assert (
            generate_fn is not None
        )  # guaranteed by the guard above; narrows for the type checker
        per_interaction = [score_interaction(p, r, generate_fn) for p, r in real]
        agg = aggregate_scores(per_interaction)
    n_infra_empty = len(empty)
    infra_empty_rate = (n_infra_empty / len(sampled)) if sampled else 0.0
    agg["n_infra_empty"] = n_infra_empty
    agg["infra_empty_rate"] = infra_empty_rate

    published: dict[str, float] = {}
    infra_published: dict[str, float] = {}
    if not dry_run:
        published = publish_online_scores(agg["scores"], writer=writer, extra_labels=extra_labels)
        infra_published = publish_infra_empty_rate(
            infra_empty_rate, writer=writer, extra_labels=extra_labels
        )
    return {
        "aggregate": agg,
        "published": published,
        "infra_published": infra_published,
        "n_captured": len(pairs),
        "n_sampled": len(sampled),
        "n_infra_empty": n_infra_empty,
        "infra_empty_rate": infra_empty_rate,
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
    """Deterministic (temperature=0) + retrying Vertex judge call (shared helper)."""
    from src.eval.judge_client import build_judge_generate_fn

    return build_judge_generate_fn(judge_model, project, location)


def capture_live_interactions(
    agent, prompts: Sequence[str], user_id: str = "online-monitor-user"
) -> list[tuple[str, str]]:
    """Drive each prompt through the deployed agent, capturing ``(prompt, response)``.

    Uses ``stream_query`` and reuses the traffic generator's ``_extract_text`` so
    the captured text matches what the traffic tooling records. This is the
    load-bearing lever: the response CONTENT is available client-side even though
    the managed runtime strips it from the trace surface.

    If the SDK's array-only REST parser can't read the engine's NDJSON stream
    (:data:`raw_stream.SSE_PARSE_MARKER` — happens on a recycled engine), this
    transparently falls back to the client-only raw-SSE reader
    (:mod:`src.eval.raw_stream`), which addresses the same engine by resource name
    and yields identical events. Unrelated errors propagate.
    """
    from src.traffic.generate_traffic import _extract_text

    pairs: list[tuple[str, str]] = []
    for prompt in prompts:
        try:
            session = agent.create_session(user_id=user_id)
            response = agent.stream_query(user_id=user_id, session_id=session["id"], message=prompt)
            text = "".join(_extract_text(chunk) for chunk in response)
        except ValueError as exc:
            resource = raw_stream.agent_resource_name(agent)
            if not raw_stream.is_sse_parse_skew(exc) or not resource:
                raise
            [(_, text)] = raw_stream.capture_pairs(resource, [prompt], user_id=user_id)
        pairs.append((prompt, text))
    return pairs


def capture_live_faithfulness(
    agent,
    prompts: Sequence[str],
    user_id: str = "online-monitor-user",
    *,
    include_transfers: bool = False,
) -> list[dict]:
    """Like :func:`capture_live_interactions`, but RETAIN the executed trajectory.

    Tool-call faithfulness needs the real ``function_call`` trajectory, which the
    ``(prompt, response)`` capture discards. This drives each prompt through the
    deployed engine once and returns ``{"prompt", "response", "actual_trajectory"}``
    triples (trajectory via :func:`src.eval.trajectory_eval.capture_trajectory`) —
    the shape :func:`src.eval.tool_faithfulness.score_cases` consumes.

    Load-bearing assumption (same Branch-A/B fork as offline, resolved by
    :mod:`src.eval.spike_trajectory_visibility`): the coordinator's client stream
    must surface nested sub-agent MCP calls. If it only surfaces
    ``transfer_to_agent``, online faithfulness is delegation-level until sub-agent
    engines or server-side capture are used — set ``include_transfers`` to audit
    the routing itself.
    """
    from src.eval.trajectory_eval import capture_trajectory
    from src.traffic.generate_traffic import _extract_text

    triples: list[dict] = []
    for prompt in prompts:
        try:
            session = agent.create_session(user_id=user_id)
            events = list(
                agent.stream_query(user_id=user_id, session_id=session["id"], message=prompt)
            )
        except ValueError as exc:
            resource = raw_stream.agent_resource_name(agent)
            if not raw_stream.is_sse_parse_skew(exc) or not resource:
                raise
            # Raw-SSE fallback already builds the {prompt, response, trajectory} shape.
            triples.extend(
                raw_stream.capture_triples(
                    resource, [prompt], user_id=user_id, include_transfers=include_transfers
                )
            )
            continue
        triples.append(
            {
                "prompt": prompt,
                "response": "".join(_extract_text(event) for event in events),
                "actual_trajectory": capture_trajectory(
                    events, include_transfers=include_transfers
                ),
            }
        )
    return triples


def score_and_publish_faithfulness(
    triples: Sequence[dict],
    *,
    generate_fn: Callable[[str], str],
    sample_rate: float = 1.0,
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Sample → drop infra-empty → grounded-judge faithfulness → publish online.

    Reuses :func:`src.eval.tool_faithfulness.score_cases` (same judge + parser as
    the offline series), then publishes the mean to
    ``agent_online_eval/tool_faithfulness`` on the shared 1-5 axis via
    :func:`publish_online_scores` (which scales 0-1 → 1-5 and filters to the
    monitored names). Empty-at-200 / error-shaped responses have nothing to audit,
    so they are excluded before judging (counted as ``n_infra_empty``). Returns
    ``{"result", "published", "n_captured", "n_sampled", "n_infra_empty"}``; a
    ``dry_run`` still computes the score but writes nothing.
    """
    from src.eval.tool_faithfulness import score_cases

    sampled = sample_interactions(triples, sample_rate)
    real = [t for t in sampled if not is_infra_empty(t.get("response", ""))]
    n_infra_empty = len(sampled) - len(real)
    result = score_cases(real, generate_fn)

    published: dict[str, float] = {}
    score = result.get("score")
    if not dry_run and score is not None:
        published = publish_online_scores(
            {"tool_faithfulness": score}, writer=writer, extra_labels=extra_labels
        )
    return {
        "result": result,
        "published": published,
        "n_captured": len(triples),
        "n_sampled": len(sampled),
        "n_infra_empty": n_infra_empty,
    }


def run_online_faithfulness(
    agent_id: str | None = None,
    *,
    n_interactions: int | None = None,
    sample_rate: float = 1.0,
    prompts: Sequence[str] | None = None,
    judge_model: str = DEFAULT_JUDGE_MODEL,
    include_transfers: bool = False,
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
    dry_run: bool = False,
    agent=None,
    generate_fn: Callable[[str], str] | None = None,
) -> dict:
    """Drive live traffic, capture trajectories, and publish online faithfulness.

    The faithfulness analogue of :func:`run_online_monitor`: it captures
    ``(prompt, response, trajectory)`` triples (not just text) so the grounded
    judge can compare claimed vs executed actions. ``agent``/``generate_fn``/
    ``writer`` are injectable for testing; the CLI wires the real deployed engine
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

    triples = capture_live_faithfulness(agent, probe, include_transfers=include_transfers)
    gen = generate_fn or _default_generate_fn(judge_model, None, None)
    return score_and_publish_faithfulness(
        triples,
        generate_fn=gen,
        sample_rate=sample_rate,
        writer=writer,
        extra_labels=extra_labels,
        dry_run=dry_run,
    )


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
    panel: bool = False,
    judges: Sequence[Callable[[str], str]] | None = None,
) -> dict:
    """Sample live coordinator traffic, score it, and publish ``agent_online_eval/*``.

    Drives the probe prompts (or ``prompts``, capped at ``n_interactions``)
    against the deployed engine, captures responses client-side, samples, scores,
    and publishes. ``agent``/``generate_fn``/``judges``/``writer`` are injectable
    so the pipeline is testable; the CLI wires the real deployed engine and the
    google.genai judge.

    With ``panel=True`` (or an explicit ``judges`` list) each response is scored by
    the diverse multi-model :func:`src.eval.judge_panel.build_panel` (median of the
    panel + inter-rater reliability) instead of the single ``judge_model``
    autorater — the same higher-trust scoring the offline judges use.
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
    if panel or judges is not None:
        from src.eval.judge_panel import build_panel

        members = judges if judges is not None else build_panel()
        return score_and_publish(
            pairs,
            judges=members,
            sample_rate=sample_rate,
            writer=writer,
            extra_labels=extra_labels,
            dry_run=dry_run,
        )
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
    n_empty = result.get("n_infra_empty", 0)
    empty_rate = result.get("infra_empty_rate", 0.0)
    reliability = agg.get("reliability")
    mode = "diverse judge panel (median)" if reliability else "single judge"
    print(
        f"\nOnline quality monitor [{mode}]: scored "
        f"{result['n_sampled']}/{result['n_captured']} captured interactions "
        f"({agg['n_interactions']} judged)"
    )
    print(
        f"  infra-empty (empty-at-200 / error-shaped, excluded from quality): "
        f"{n_empty}/{result['n_sampled']} ({empty_rate:.1%})"
    )
    for name in RUBRIC_BUILDERS:  # only the (prompt, response) rubrics this path scores
        mean = agg["scores"].get(name)
        count = agg["counts"].get(name, 0)
        if mean is None:
            print(f"  {name}: n/a (0 scored)")
        else:
            ci = agg.get("ci", {}).get(name)
            ci_str = f"  95% CI [{ci[0]:.3f}, {ci[1]:.3f}]" if ci else ""
            flag = "  ⚠ low_confidence" if agg.get("low_confidence", {}).get(name) else ""
            print(
                f"  {name}: {mean:.3f} (0-1) → {_to_monitored_scale(mean)} (1-5) "
                f"over {count}{ci_str}{flag}"
            )
            rel = (reliability or {}).get(name)
            if rel and rel.get("n_judges", 0) >= 2:
                alpha = rel.get("alpha")
                alpha_str = "nan" if alpha is None or alpha != alpha else f"{alpha:.3f}"
                print(
                    f"      panel IRR: Krippendorff alpha={alpha_str}, "
                    f"mean spread={rel.get('mean_spread', 0.0):.3f} "
                    f"over {rel.get('n_judges')} judges"
                )
    prefix = "[dry-run] would publish" if dry_run else "published"
    print(f"{prefix}: {json.dumps(result['published'], indent=2, sort_keys=True)}")


def _print_faithfulness_summary(result: dict, *, dry_run: bool) -> None:
    inner = result["result"]
    score = inner.get("score")
    scaled = f"{_to_monitored_scale(score)} (1-5)" if score is not None else "n/a"
    print(
        f"\nOnline tool-call faithfulness: scored {inner.get('n_scored')}/"
        f"{inner.get('n_total')} of {result['n_sampled']}/{result['n_captured']} "
        f"captured interactions ({result.get('n_infra_empty', 0)} infra-empty excluded)"
    )
    print(f"  tool_faithfulness: {score if score is not None else 'n/a'} (0-1) → {scaled}")
    flagged = inner.get("flagged") or []
    if flagged:
        print(f"  {len(flagged)} response(s) with hallucinated actions:")
        for item in flagged:
            prompt = str(item.get("prompt", ""))[:70]
            print(f"    - [{', '.join(item.get('hallucinated', []))}]  «{prompt}»")
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
        "--panel",
        action="store_true",
        help="score the quality rubrics with the DIVERSE MULTI-MODEL JUDGE PANEL "
        "(median of gemini-2.5-flash + gemini-3.5-flash + "
        "inter-rater reliability) instead of the single --judge-model autorater; "
        "does not apply to --faithfulness",
    )
    parser.add_argument(
        "--faithfulness",
        action="store_true",
        help="score TOOL-CALL FAITHFULNESS (claimed vs actually-executed tools) instead of the "
        "quality rubrics → agent_online_eval/tool_faithfulness (requires --agent-id; the "
        "trajectory is captured live via stream_query, not available from --from-json pairs)",
    )
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

    if args.faithfulness:
        # Faithfulness needs the live executed trajectory, which pre-captured
        # (prompt, response) pairs from --from-json don't carry.
        if args.from_json:
            parser.error("--faithfulness requires live capture (--agent-id), not --from-json pairs")
        result = run_online_faithfulness(
            agent_id=args.agent_id,
            n_interactions=args.samples,
            sample_rate=args.sample_rate,
            judge_model=args.judge_model,
            extra_labels=extra_labels,
            dry_run=args.dry_run,
        )
        _print_faithfulness_summary(result, dry_run=args.dry_run)
        return 0

    if args.from_json:
        pairs = _load_pairs(args.from_json)
        if args.panel:
            from src.eval.judge_panel import build_panel

            result = score_and_publish(
                pairs,
                judges=build_panel(),
                sample_rate=args.sample_rate,
                extra_labels=extra_labels,
                dry_run=args.dry_run,
            )
        else:
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
            panel=args.panel,
        )

    _print_summary(result, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
