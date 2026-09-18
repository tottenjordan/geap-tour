"""Measure the coordinator's empty-stream RATE — the router check's twin.

``demo_readiness.check_engine_live`` asks "does this engine answer at all": three
attempts, pass on the first success. That is the right question for a *wedged*
engine and the wrong one for a *flaky* one, and the difference is not academic.
Measured 2026-09-18 (``docs/empty_at_200_analysis.md``), both live coordinator
engines return empty-at-200 at **8-19%**:

    engine        empty rate    check_engine_live passes
    probe 4380           19%                      99.3%
    pinned 3639           8%                      99.9%

So the readiness gate goes green on an engine that will visibly fail on stage:

    demo length     P(at least one empty response) at 19%
     5 turns                                         65%
    10 turns                                         88%
    20 turns                                         99%

This module asks the other question. It reuses the router health check's machinery
wholesale — :func:`~src.eval.verify_router_health.classify_outcome`,
:func:`~src.eval.verify_router_health.summarize`,
:func:`~src.eval.verify_router_health.verdict`, the raw-SSE transport, the Wilson
interval — because none of it was ever router-specific. Only the probe set is, and
this supplies a coordinator-shaped one.

**Grouped by CAPABILITY, not tier.** The router's probes span model tiers because
that is what varies there. The coordinator holds three MCP toolsets plus Memory
Bank, so its probes span *those*: a flaky booking path and a flaky memory path are
different problems, and the grouped output says which.

Silent empties are counted apart from labelled ones, exactly as in the router
check: ``RetryingLlm`` turning silence into a greppable ``EMPTY_RESPONSE`` marker
is a real improvement, and conflating the two would hide it.

Run::

    uv run python -m src.eval.verify_coordinator_health --agent-id <ENGINE_ID>
    uv run python -m src.eval.verify_coordinator_health --agent-id <ID> --repeat 3 --threshold 0.05
    uv run python -m src.eval.verify_coordinator_health --agent-id <ID> --json
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING, Any

from src.eval.types import HealthVerdict, RateSummary
from src.eval.verify_router_health import (
    DEFAULT_THRESHOLD,
    run_probes,
    summarize,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

#: Coordinator probes, one per capability it actually holds. Kept short and
#: single-intent on purpose: a multi-step prompt that empties out tells you the
#: coordinator is flaky but not *where*, and the point of the grouping is to say
#: where. The labels become the ``by_tier`` buckets in the shared summary.
PROBES: list[tuple[str, str]] = [
    ("search", "Find flights from SFO to JFK next Monday."),
    ("search", "Find hotels in New York under $350 per night."),
    ("booking", "List all my recent bookings."),
    ("booking", "Book flight FL001 for Alice Johnson."),
    ("expense", "Is a $450 client dinner within our expense policy?"),
    ("expense", "Show expense history for EMP001."),
    ("memory", "Remind me of my saved travel preferences."),
    ("plain", "What can you help me with?"),
]

#: Same ceiling as the router. A demo that drops one turn in twenty is visibly
#: broken on stage, so this is deliberately tight — and note that both engines
#: measured on 2026-09-18 FAIL it. That is the gate working, not miscalibrated:
#: it is saying "do not demo on this engine yet".
COORDINATOR_THRESHOLD = DEFAULT_THRESHOLD


def check_health(
    agent_id: str,
    *,
    repeat: int = 2,
    threshold: float = COORDINATOR_THRESHOLD,
    probes: Sequence[tuple[str, str]] | None = None,
    verbose: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    """Probe the coordinator ``repeat`` times per prompt; return summary + verdict.

    ``kwargs`` forwards the router check's injectable seams (``stream_fn``,
    ``session_fn``, ``sleep``) so this is testable with no engine.
    """
    from src.eval.batch_eval import _resolve_agent_resource_name

    results = run_probes(
        _resolve_agent_resource_name(agent_id),
        repeat=repeat,
        probes=list(probes) if probes is not None else PROBES,
        verbose=verbose,
        **kwargs,
    )
    summary = summarize(results)
    return {
        "engine": agent_id,
        "summary": summary,
        "verdict": three_valued_verdict(summary, threshold=threshold),
        "results": results,
    }


def three_valued_verdict(summary: RateSummary, *, threshold: float) -> HealthVerdict:
    """PASS / FAIL / INCONCLUSIVE on the silent-empty rate.

    The router's binary :func:`~src.eval.verify_router_health.verdict` compares a
    point estimate, and at readiness sample sizes that is a coin flip: a live run
    read 1/16 = 6.2% with a 95% interval of **[1.1%, 28.3%]** — an interval that
    straddles the 5% ceiling, so "FAIL" and "PASS" were both defensible from the
    same data. A gate that flips on noise gets muted, which is the lesson the
    ``instruction_following`` floor already taught this repo.

    So the interval decides, following the three-valued pattern
    :mod:`src.eval.calibration` established:

    ``FAIL``
        The interval's LOWER bound clears the ceiling — the engine is confidently
        too flaky to demo on. Exits non-zero.
    ``PASS``
        The UPPER bound is under the ceiling — confidently fine.
    ``INCONCLUSIVE``
        The interval spans the ceiling. Exits **zero** and names the sample size
        that would settle it, because failing here would red the gate on noise and
        passing silently would hide a real risk. Saying "I don't know, run N more"
        is the only honest third option.

    Zero samples is never a pass — an absent measurement is not a clean one.
    """
    n = summary.get("n", 0)
    if not n:
        return {
            "passed": False,
            "status": "FAIL",
            "threshold": threshold,
            "reason": "no samples — an absent measurement is not a clean one",
        }

    rate = summary.get("empty_rate", 0.0)
    lo, hi = summary.get("empty_rate_ci", (0.0, 0.0))
    if lo > threshold:
        status, passed = "FAIL", False
        reason = f"silent empty {rate:.1%}, 95% CI [{lo:.1%}, {hi:.1%}] is entirely above {threshold:.0%}"
    elif hi <= threshold:
        status, passed = "PASS", True
        reason = f"silent empty {rate:.1%}, 95% CI upper {hi:.1%} is within {threshold:.0%}"
    else:
        status, passed = "INCONCLUSIVE", True
        reason = (
            f"silent empty {rate:.1%}, but 95% CI [{lo:.1%}, {hi:.1%}] spans the "
            f"{threshold:.0%} ceiling at n={n} — {_n_needed(rate, threshold)}"
        )
    return {"passed": passed, "status": status, "threshold": threshold, "reason": reason}


def _n_needed(rate: float, threshold: float) -> str:
    """Roughly how many turns would separate ``rate`` from ``threshold``.

    A normal-approximation sample size. Deliberately reported as an order of
    magnitude ("~200 turns"), not a precise figure — the input is itself a noisy
    estimate, and false precision here would be the exact error this function
    exists to avoid.
    """
    delta = abs(rate - threshold)
    if delta < 1e-6:
        return "the observed rate sits on the ceiling; more turns cannot separate them"
    n = int(2 * (1.96**2) * max(rate, threshold) * (1 - max(rate, threshold)) / (delta**2))
    rounded = max(50, round(n, -2))
    return f"~{rounded} turns would settle it"


def format_report(report: dict[str, Any]) -> str:
    """Report the RATE and its interval, never a bare count.

    "1 empty in 8" is not 12.5% in any useful sense — at demo sample sizes the
    interval is most of the range, and printing the point estimate alone invites
    a decision the data cannot support.
    """
    s, v = report["summary"], report["verdict"]
    lo, hi = s["empty_rate_ci"]
    lines = [
        "=" * 60,
        "COORDINATOR HEALTH",
        "=" * 60,
        f"  turns:            {s['n']}",
        f"  full:             {s['counts']['FULL']} ({s['full_rate']:.1%})",
        f"  silent empty:     {s['silent_empty']} ({s['empty_rate']:.1%}) "
        f"95% CI [{lo:.1%}, {hi:.1%}]",
        f"  labelled failure: {s['labelled_failure']} "
        f"(throttled={s['counts']['THROTTLED']}, empty={s['counts']['EMPTY_LABELLED']})",
        f"  latency p50/p95:  {s['p50_latency_s']:.1f}s / {s['p95_latency_s']:.1f}s",
    ]
    if s.get("skipped"):
        lines.append(f"  skipped:          {s['skipped']} (excluded from every rate)")
    if s.get("by_tier"):
        lines.append("  by capability:")
        for name, r in s["by_tier"].items():
            lines.append(
                f"    {name:<10} {r['silent_empty']}/{r['n']} empty ({r['empty_rate']:.0%})"
            )
    lines += [
        "-" * 60,
        f"  {v['status']}: {v['reason']}",
    ]
    if v["status"] == "FAIL":
        lines += [
            "",
            "  A demo on this engine will visibly fail: at a 19% empty rate a",
            "  10-turn demo has an ~88% chance of at least one blank response.",
            "  Cause unknown as of 2026-09-18 — see docs/empty_at_200_analysis.md.",
        ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-id", help="engine to probe (default: AGENT_ENGINE_ID)")
    parser.add_argument("--repeat", type=int, default=2, help="Passes over the probe set")
    parser.add_argument(
        "--threshold",
        type=float,
        default=COORDINATOR_THRESHOLD,
        help="Max silent-empty rate before this fails",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv)

    from src.config import AGENT_ENGINE_ID

    agent_id = args.agent_id or AGENT_ENGINE_ID
    if not agent_id:
        raise SystemExit("no --agent-id and AGENT_ENGINE_ID is unset")

    report = check_health(
        agent_id, repeat=args.repeat, threshold=args.threshold, verbose=not args.json
    )
    print(json.dumps(report, indent=2, default=str) if args.json else format_report(report))
    return 0 if report["verdict"]["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
