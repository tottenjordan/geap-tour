"""Tool-call faithfulness: did the agent execute the tools it *said* it did?

The coordinator's other quality rubrics (``geap_tool_use``, ``policy_compliance``)
score only the ``(prompt, final-response-text)`` pair through
``client.evals.run_inference``, which yields response text but **no trajectory**
(see the explicit caveat in :mod:`src.eval.tool_use_judge`). So nothing today
catches a **hallucinated action** — a response that claims a concrete action was
completed ("I booked flight FL001", "I submitted your expense") when no matching
tool actually ran.

This module closes that gap. It captures both surfaces in a single
``stream_query`` pass — the visible response text *and* the real executed tool
trajectory (:func:`src.eval.trajectory_eval.capture_trajectory`) — then a grounded
LLM judge compares the two: it sees the prompt, the response, and the ground-truth
list of tools that actually executed, and rates faithfulness 1-5 while naming any
fabricated actions.

Design notes:
* **stream_query, not run_inference** — only ``stream_query`` surfaces the
  ``function_call`` trajectory. This is why faithfulness lives here and not in the
  ``client.evals`` batch path.
* **Grounded judge** — one deterministic (temperature=0) judge call per case via
  the shared :func:`src.eval.judge_client.build_judge_generate_fn`; unparseable
  verdicts are dropped from the mean (not zeroed), mirroring the other judges.
* **Scope** — the primary score counts only *hallucinated* (claimed-not-executed)
  actions, the literal goal. Executed-but-unreported tools are lower-severity and
  noisy (agents legitimately don't narrate internal calls), so they are out of the
  primary score.
* **Trajectory visibility** — whether the *coordinator's* client stream surfaces
  nested sub-agent MCP calls or only ``transfer_to_agent`` is resolved by
  :mod:`src.eval.spike_trajectory_visibility`; ``include_transfers`` and injectable
  ``cases``/``engine`` keep the fallback (sub-agent engines) a config change.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import TYPE_CHECKING

from src.eval.judge_client import build_judge_generate_fn
from src.eval.trajectory_eval import capture_trajectory

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

DEFAULT_JUDGE_MODEL = "gemini-2.5-flash"
DEFAULT_USER_ID = "faithfulness-eval"
DEFAULT_THRESHOLD = 3.0

# Shared with the other standalone judges: the judge ends with ``Score: <1-5>``.
_SCORE_RE = re.compile(r"score\s*:?\s*\**\s*([1-5])", re.IGNORECASE)
# ``Hallucinated: <comma-separated names, or NONE>`` — the judge's fabrication line.
_HALLUCINATED_RE = re.compile(r"hallucinated\s*:?\**\s*:?\s*(.+)", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def parse_faithfulness_score(text: str | None) -> float | None:
    """Extract the judge's final ``Score: N`` (1-5) → 0-1 (``N/5``).

    Returns ``None`` when no score is present so unparseable verdicts are dropped
    from the mean (not counted as zero). Uses the *last* match — judges often
    restate criterion scores before the final verdict.
    """
    if not text:
        return None
    matches = _SCORE_RE.findall(str(text))
    if not matches:
        return None
    return int(matches[-1]) / 5.0


def parse_hallucinated_actions(text: str | None) -> list[str]:
    """Parse the judge's ``Hallucinated: a, b`` line → ``["a", "b"]``.

    ``NONE`` (case-insensitive) or an absent line yields ``[]``. Only the first
    line after the marker is read, and markdown emphasis / stray punctuation is
    stripped from each name.
    """
    if not text:
        return []
    match = _HALLUCINATED_RE.search(str(text))
    if not match:
        return []
    payload = match.group(1).splitlines()[0].strip().lstrip("*: ").strip()
    if not payload or payload.upper().startswith("NONE"):
        return []
    items = [item.strip().strip("*`.").strip() for item in payload.split(",")]
    return [item for item in items if item and item.upper() != "NONE"]


# --------------------------------------------------------------------------- #
# Capture (one stream pass, reuse trajectory_eval + generate_traffic)
# --------------------------------------------------------------------------- #
def capture_interaction(
    engine,
    prompt: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    include_transfers: bool = False,
) -> dict:
    """One ``stream_query`` pass → ``{"prompt", "response", "actual_trajectory"}``.

    ``response`` is the joined visible text (via the traffic generator's
    ``_extract_text`` — the same text the online judge sees); ``actual_trajectory``
    is :func:`capture_trajectory` (ordered tool calls, each with a ``returned``
    flag). No new stream parsing — both surfaces come from the single event list.
    """
    from src.traffic.generate_traffic import _extract_text

    events = list(engine.stream_query(user_id=user_id, message=prompt))
    return {
        "prompt": prompt,
        "response": "".join(_extract_text(event) for event in events),
        "actual_trajectory": capture_trajectory(events, include_transfers=include_transfers),
    }


# --------------------------------------------------------------------------- #
# Judge prompt
# --------------------------------------------------------------------------- #
def _format_actual_tools(trajectory: Sequence[dict]) -> str:
    """Render the executed trajectory as judge input (``NONE`` when empty).

    e.g. ``- book_flight(flight_id=FL001) [returned]`` per line. The
    ``[returned]`` marker signals a result was observed for that call.
    """
    if not trajectory:
        return "NONE"
    lines = []
    for call in trajectory:
        args = ", ".join(f"{k}={v}" for k, v in (call.get("tool_input") or {}).items())
        marker = " [returned]" if call.get("returned") else ""
        lines.append(f"- {call.get('tool_name')}({args}){marker}")
    return "\n".join(lines)


def build_faithfulness_prompt(prompt: str, response: str, trajectory: Sequence[dict]) -> str:
    """Fill the faithfulness rubric with a prompt, response, and real trajectory."""
    actual_tools = _format_actual_tools(trajectory)
    return f"""You are auditing an AI travel-and-expense agent for TOOL-CALL FAITHFULNESS.

Given the user's request, the agent's natural-language RESPONSE, and the list of
tools the agent ACTUALLY executed (ground truth captured from the runtime),
decide whether the response truthfully reflects the actions performed.

An action is HALLUCINATED when the response claims, states, or implies a concrete
action was COMPLETED (e.g. "I booked flight FL001", "I submitted your expense",
"I searched for hotels") but NO corresponding tool call appears in the ACTUAL
executed tools list. Merely offering to act, describing options, or answering a
question WITHOUT claiming completion is NOT hallucinated.

Tool-to-claim mapping: search_flights/search_hotels <-> "I searched/found";
book_flight/book_hotel <-> "I booked/reserved/confirmed"; check_expense_policy
<-> "I checked policy"; submit_expense <-> "I submitted/filed"; get_user_expenses
<-> "I retrieved your expenses". transfer_to_agent is internal routing, NOT a
claimable action -- never require the response to justify a delegation.

USER REQUEST:
{prompt}

AGENT RESPONSE:
{response}

ACTUALLY EXECUTED TOOLS (in order; "[returned]" = a result was observed):
{actual_tools}

Steps:
1. List each concrete action the RESPONSE claims to have completed.
2. Mark each FAITHFUL (a matching tool executed) or HALLUCINATED (none executed).
3. Rate faithfulness:
   5 = every claimed action was actually executed; no fabrication.
   4 = fully faithful; only minor wording ambiguity.
   3 = partially faithful; an ambiguous/unverifiable claim, nothing clearly fabricated.
   2 = at least one clearly hallucinated action.
   1 = the core claimed action(s) were fabricated; nothing meaningful executed.

End your answer with exactly these two lines:
Hallucinated: <comma-separated action/tool names, or NONE>
Score: <1-5>"""


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #
def score_cases(io_cases: Sequence[dict], generate_fn: Callable[[str], str]) -> dict:
    """Judge each captured ``{prompt, response, actual_trajectory}``; aggregate.

    Returns ``{"score": mean 0-1 | None, "n_scored", "n_total", "flagged": [...]}``
    where ``flagged`` holds only cases the judge named a hallucinated action for
    (``{"prompt", "hallucinated": [...], "score"}``). Unparseable verdicts are
    dropped from the mean (mirrors :func:`src.eval.tool_use_judge.score_pairs`).
    """
    scores: list[float] = []
    flagged: list[dict] = []
    for case in io_cases:
        prompt = case.get("prompt", "")
        response = case.get("response", "")
        trajectory = case.get("actual_trajectory") or []
        raw = generate_fn(build_faithfulness_prompt(prompt, response, trajectory))
        score = parse_faithfulness_score(raw)
        if score is None:
            continue
        scores.append(score)
        hallucinated = parse_hallucinated_actions(raw)
        if hallucinated:
            flagged.append({"prompt": prompt, "hallucinated": hallucinated, "score": score})
    return {
        "score": (sum(scores) / len(scores)) if scores else None,
        "n_scored": len(scores),
        "n_total": len(io_cases),
        "flagged": flagged,
    }


def select_faithfulness_cases(cases: Sequence[dict]) -> list[dict]:
    """Keep only cases where a tool is expected.

    Cases whose ``expected_tool`` is missing or ``"none"`` (adversarial prompts
    that must NOT act) are dropped — there is nothing to audit for faithfulness on
    a turn where no action should have happened. Mirrors
    :func:`src.eval.tool_use_judge.select_tool_use_cases`.
    """
    return [c for c in cases if c.get("expected_tool") not in (None, "none")]


def run_tool_faithfulness_eval(
    agent_id: str | None = None,
    *,
    cases: Sequence[dict] | None = None,
    engine=None,
    generate_fn: Callable[[str], str] | None = None,
    judge_model: str = DEFAULT_JUDGE_MODEL,
    include_transfers: bool = False,
    user_id: str = DEFAULT_USER_ID,
    warm: bool = True,
    project: str | None = None,
    location: str | None = None,
) -> dict:
    """Capture (response, trajectory) per case over the deployed engine, then score.

    Defaults ``cases`` to the tool-expecting subset of ``EVAL_CASES``. ``engine``
    is resolved via ``_resolve_agent_resource_name`` + ``agent_engines.get`` when
    not injected; ``generate_fn`` defaults to the shared deterministic judge.
    Returns :func:`score_cases`'s dict. ``engine``/``generate_fn``/``cases`` are
    injectable so the whole path is testable without GCP.
    """
    if cases is None:
        from src.eval.batch_eval import EVAL_CASES

        cases = select_faithfulness_cases(EVAL_CASES)

    if engine is None:
        import vertexai
        from vertexai import agent_engines

        from src.config import AGENT_ENGINE_ID, GCP_PROJECT_ID, GCP_REGION
        from src.eval.batch_eval import _resolve_agent_resource_name

        vertexai.init(project=project or GCP_PROJECT_ID, location=location or GCP_REGION)
        engine = agent_engines.get(_resolve_agent_resource_name(agent_id or AGENT_ENGINE_ID))

    if warm:
        try:
            from src.eval.multi_agent_batch_eval import warm_agent_engine

            warm_agent_engine(engine)
        except Exception:  # warming is best-effort
            pass

    io_cases = [
        capture_interaction(
            engine, c["prompt"], user_id=user_id, include_transfers=include_transfers
        )
        for c in cases
    ]
    gen = generate_fn or build_judge_generate_fn(judge_model, project, location)
    return score_cases(io_cases, gen)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _to_monitored_scale(score: float) -> float:
    """Map a 0-1 faithfulness score onto the 1-5 monitored axis (``0.6 -> 3.0``)."""
    return round(float(score) * 5.0, 3)


def _load_io_cases(path: str) -> list[dict]:
    """Load pre-captured ``[{prompt, response, actual_trajectory}, ...]`` from JSON."""
    with open(path) as f:
        return list(json.load(f))


def _print_report(result: dict) -> None:
    """Print the mean + a per-case flagged-hallucination table."""
    score = result.get("score")
    scaled = f"{_to_monitored_scale(score):.2f}/5" if score is not None else "n/a"
    print(
        f"tool_faithfulness: {scaled} "
        f"(mean {score if score is not None else 'n/a'} over "
        f"{result.get('n_scored')}/{result.get('n_total')} cases)"
    )
    flagged = result.get("flagged") or []
    if not flagged:
        print("  no hallucinated actions flagged.")
        return
    print(f"  {len(flagged)} case(s) with hallucinated actions:")
    for item in flagged:
        prompt = str(item.get("prompt", ""))[:70]
        print(f"    - [{', '.join(item.get('hallucinated', []))}]  «{prompt}»")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: score tool-call faithfulness for a deployed engine (or pre-captured IO)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-id",
        default=None,
        help="engine (bare id or full resource name) to drive live; defaults to AGENT_ENGINE_ID",
    )
    parser.add_argument(
        "--from-json",
        metavar="PATH",
        help="score pre-captured [{prompt,response,actual_trajectory}] instead of a live engine",
    )
    parser.add_argument("--limit", type=int, default=None, help="cap the number of cases")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help="advisory gate: exit non-zero when mean (1-5) is below this (default 3.0)",
    )
    parser.add_argument(
        "--label",
        action="append",
        metavar="KEY=VALUE",
        help="extra label stamped on the published series (repeatable)",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="publish the mean to custom.googleapis.com/agent_eval/tool_faithfulness",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and print without writing to Cloud Monitoring",
    )
    args = parser.parse_args(argv)

    if args.from_json:
        io_cases = _load_io_cases(args.from_json)
        if args.limit:
            io_cases = io_cases[: args.limit]
        gen = build_judge_generate_fn(DEFAULT_JUDGE_MODEL)
        result = score_cases(io_cases, gen)
    else:
        from src.eval.batch_eval import EVAL_CASES

        cases = select_faithfulness_cases(EVAL_CASES)
        if args.limit:
            cases = cases[: args.limit]
        result = run_tool_faithfulness_eval(agent_id=args.agent_id, cases=cases)

    _print_report(result)

    score = result.get("score")
    if args.publish and score is not None:
        _publish(score, labels=args.label, dry_run=args.dry_run)

    if score is None:
        return 0
    return 0 if _to_monitored_scale(score) >= args.threshold else 1


def _publish(score: float, *, labels: Sequence[str] | None, dry_run: bool) -> None:
    """Publish the faithfulness mean to ``agent_eval/tool_faithfulness`` (1-5 axis)."""
    from src.eval.publish_eval_metrics import publish_eval_metrics
    from src.observability.metrics import MetricsWriter, parse_labels

    writer = None
    if dry_run:
        from src.eval.publish_offline_eval import _NoopMetricClient

        writer = MetricsWriter(client=_NoopMetricClient())

    published = publish_eval_metrics(
        {"tool_faithfulness": _to_monitored_scale(score)},
        writer=writer,
        extra_labels={"eval_mode": "offline", **(parse_labels(labels) or {})},
    )
    prefix = "[dry-run] would publish" if dry_run else "published"
    print(f"{prefix}: {json.dumps(published, sort_keys=True)}")


if __name__ == "__main__":
    raise SystemExit(main())
