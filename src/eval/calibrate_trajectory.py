"""Diagnostic: can a trajectory metric actually be satisfied by our agent?

Trajectory scoring compares tool calls **literally**. ADK's ``TrajectoryEvaluator``
checks ``actual.name == expected.name`` *and* ``actual.args == expected.args`` in all
three match types (``EXACT`` / ``IN_ORDER`` / ``ANY_ORDER``), with no normalization
hook. Before making that a scored criterion — in the eval run or, worse, in the GEPA
objective — we need to know what fraction of cases the agent can actually satisfy.
A criterion the agent cannot reach makes the optimizer chase noise.

Two known sources of mismatch this measures separately:

1. **Names.** Runtime emits ``booking_mcp_book_flight`` when the Agent Registry
   resolves the toolset, bare ``book_flight`` on the direct-URL fallback. The curated
   references use bare names. :func:`src.eval.trajectory_eval.normalize_tool_name`
   fixes this deterministically — reported as before/after.
2. **Args.** The reference lists only tool *names*
   (``batch_eval.EVAL_CASES[*].reference_trajectory``), so any args comparison is
   against what the model chose to send. This is the risk that cannot be fixed by
   normalization, and the reason this diagnostic exists.

**Read-only**: drives the engine, writes no metrics and no files.

Usage:
  uv run python -m src.eval.calibrate_trajectory --agent-id <ENGINE_ID>
  uv run python -m src.eval.calibrate_trajectory --agent-id <ENGINE_ID> --limit 12
"""

from __future__ import annotations

import argparse
import collections
from typing import TYPE_CHECKING

from src.eval.trajectory_eval import normalize_tool_name

if TYPE_CHECKING:
    from collections.abc import Sequence

MATCH_TYPES = ("EXACT", "IN_ORDER", "ANY_ORDER")


def _names(calls: list[dict], *, normalize: bool) -> list[str]:
    out = [str(c["tool_name"]) for c in calls]
    if not normalize:
        return out
    # normalize_tool_name passes None/"" through; call names are never empty here.
    return [str(normalize_tool_name(n)) for n in out]


def matches(actual: list[str], expected: list[str], match_type: str) -> bool:
    """Reimplements ADK's three match types over tool-name sequences.

    Mirrors ``google.adk.evaluation.trajectory_evaluator``: ``EXACT`` is a perfect
    sequence match; ``IN_ORDER`` requires every expected call to appear in order with
    extras allowed; ``ANY_ORDER`` requires every expected call to appear, order-free.
    Reimplemented rather than imported because ADK's evaluator only accepts full
    ``Invocation`` objects, and we want to score names and names+args separately.
    """
    if match_type == "EXACT":
        return actual == expected
    if match_type == "IN_ORDER":
        it = iter(actual)
        return all(any(a == e for a in it) for e in expected)
    if match_type == "ANY_ORDER":
        pool = collections.Counter(actual)
        for e in expected:
            if not pool[e]:
                return False
            pool[e] -= 1
        return True
    raise ValueError(f"unknown match type: {match_type}")


def summarize(rows: list[dict]) -> dict:
    """Per-match-type pass rates, raw vs normalized names."""
    out: dict[str, dict[str, float]] = {}
    for mt in MATCH_TYPES:
        for label, key in (("raw", "actual_raw"), ("normalized", "actual_norm")):
            hits = sum(1 for r in rows if matches(r[key], r["expected"], mt))
            out.setdefault(mt, {})[label] = hits / len(rows) if rows else 0.0
    return out


def _arg_report(rows: list[dict]) -> list[str]:
    """Why an args comparison would fail, most common first."""
    reasons: collections.Counter[str] = collections.Counter()
    for r in rows:
        for call in r["calls"]:
            args = call.get("tool_input") or {}
            if not args:
                reasons["agent sent no args"] += 1
                continue
            for k, v in args.items():
                if isinstance(v, float) and v.is_integer():
                    reasons[f"float-vs-int arg ({k}={v})"] += 1
        # the reference carries names only, so every arg the agent sends is "extra"
        if any((c.get("tool_input") or {}) for c in r["calls"]):
            reasons["reference has no args to compare against"] += 1
    return [f"{n:>4}x  {why}" for why, n in reasons.most_common(8)]


def run(engine, cases: Sequence[dict], *, user_id: str = "trajectory-calibrate") -> dict:
    """Drive each case once and collect actual vs expected tool sequences."""
    from src.eval.spike_trajectory_visibility import stream_events
    from src.eval.trajectory_eval import capture_trajectory

    rows: list[dict] = []
    for case in cases:
        events = stream_events(engine, case["prompt"], user_id=user_id)
        calls = capture_trajectory(events)
        rows.append(
            {
                "prompt": case["prompt"],
                "calls": calls,
                "actual_raw": _names(calls, normalize=False),
                "actual_norm": _names(calls, normalize=True),
                "expected": list(case["reference_trajectory"]),
            }
        )
    return {"rows": rows, "match_rates": summarize(rows)}


def render(result: dict) -> str:
    rows = result["rows"]
    prefixed = sum(1 for r in rows for n in r["actual_raw"] if "_mcp_" in n)
    bare = sum(1 for r in rows for n in r["actual_raw"] if "_mcp_" not in n)
    lines = [
        "=" * 64,
        "TRAJECTORY CALIBRATION",
        "=" * 64,
        f"  cases driven      : {len(rows)}",
        f"  tool calls seen   : {prefixed + bare}  (prefixed={prefixed}, bare={bare})",
        "",
        "  name-sequence match rate (args NOT compared):",
        f"    {'match type':<12}{'raw':>10}{'normalized':>14}",
    ]
    for mt, vals in result["match_rates"].items():
        lines.append(f"    {mt:<12}{vals['raw']:>9.0%}{vals['normalized']:>14.0%}")
    lines += ["", "  why an args comparison would fail:"]
    lines += [f"    {line}" for line in _arg_report(rows)] or ["    (none)"]
    lines += ["", "  per case (expected -> actual, normalized):"]
    for r in rows:
        ok = "OK " if matches(r["actual_norm"], r["expected"], "IN_ORDER") else "MISS"
        lines.append(f"    [{ok}] {r['prompt'][:44]}")
        lines.append(f"           exp={r['expected']}")
        lines.append(f"           act={r['actual_norm']}")
    best = result["match_rates"]["IN_ORDER"]["normalized"]
    misses = [r for r in rows if not matches(r["actual_norm"], r["expected"], "IN_ORDER")]
    empty = [r for r in misses if not r["actual_norm"]]
    real = len(misses) - len(empty)
    scorable = [r for r in rows if r["actual_norm"]]
    on_scorable = (
        sum(1 for r in scorable if matches(r["actual_norm"], r["expected"], "IN_ORDER"))
        / len(scorable)
        if scorable
        else 0.0
    )
    lines += [
        "",
        "  miss breakdown (IN_ORDER, normalized):",
        f"    {len(empty):>4}  empty trajectory  (infra-empty turn, not an ordering error)",
        f"    {real:>4}  genuine mismatch",
        f"    IN_ORDER over turns that produced ANY tool call: {on_scorable:.0%}",
        "",
        f"  VERDICT: IN_ORDER + normalized names = {best:.0%}",
        (
            "  -> above the 0.6 gate; a name-based trajectory criterion is viable."
            if best >= 0.6
            else "  -> BELOW the 0.6 gate; do not enable the optimizer criterion."
        ),
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", default=None, help="engine bare id or full resource name")
    parser.add_argument("--limit", type=int, default=None, help="cap the number of cases")
    parser.add_argument("--user-id", default="trajectory-calibrate")
    args = parser.parse_args(argv)

    import vertexai
    from vertexai import agent_engines

    from src.config import AGENT_ENGINE_ID, GCP_PROJECT_ID, GCP_REGION
    from src.eval.batch_eval import EVAL_CASES, _resolve_agent_resource_name

    cases = [c for c in EVAL_CASES if c.get("reference_trajectory")]
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("No coordinator case carries a reference_trajectory — nothing to calibrate.")
        return 1

    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    engine = agent_engines.get(_resolve_agent_resource_name(args.agent_id or AGENT_ENGINE_ID))
    print(render(run(engine, cases, user_id=args.user_id)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
