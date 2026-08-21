"""Verify the deployed router answers — and measure its empty-stream rate.

An **empty-at-200** is the router's worst failure mode: HTTP 200, a stream that
yields events, and zero characters of text. It is indistinguishable from a bad
answer to any eval that scores response text, so it has to be measured directly.

Two historically distinct causes, both now handled in
:mod:`src.models.quota_retry` and both reported separately here:

``EMPTY``
    Silent. No text, no marker — the failure mode this tool exists to drive to
    zero.
``THROTTLED`` / ``EMPTY_LABELLED``
    The retry wrapper gave up but said so, so the user sees a labelled infra
    failure instead of silence. Still a failed turn, but a *diagnosable* one —
    counted apart from the silent rate.

The probe set spans the complexity range so every tier is exercised, and the
empty rate is reported with a **Wilson interval** (:mod:`src.eval.stats`): at
demo sample sizes a bare "4%" from 1/24 is false precision.

Uses the raw-SSE client (:mod:`src.eval.raw_stream`) rather than the SDK, which
cannot parse a recycled engine's NDJSON (memory ``agent-engine-sse-parse-skew``)
— a parse error would otherwise be miscounted as an empty stream.

Run::

    uv run python -m src.eval.verify_router_health --agent-id 6134089059699523584
    uv run python -m src.eval.verify_router_health --agent-id <ID> --repeat 5 --threshold 0.05
    uv run python -m src.eval.verify_router_health --agent-id <ID> --json
"""

from __future__ import annotations

import argparse
import json
import time
from typing import TYPE_CHECKING, Any

from src.eval.stats import wilson_ci
from src.models.quota_retry import EMPTY_RESPONSE_PREFIX, THROTTLED_RESPONSE_PREFIX

if TYPE_CHECKING:
    from collections.abc import Sequence

# Prompts spanning the complexity range so the classifier exercises every tier.
# The label is the *expected* band (the live tier is the classifier's call, not
# ours) and is used only to group the breakdown.
PROBES: list[tuple[str, str]] = [
    ("lite", "What is the meal expense limit?"),
    ("lite", "Show all expenses for user EMP001"),
    ("flash", "Search for flights from SFO to JFK next Monday."),
    ("flash", "Find hotels in New York under $350 per night."),
    ("pro", "Compare flights SFO->JFK vs SFO->BOS and tell me which is cheaper."),
    (
        "high",
        "Book flight FL001 for Alice Johnson, then find a hotel in New York under $350.",
    ),
    (
        "high",
        "Show expense history for EMP001, check the entertainment policy limit, "
        "and submit a $45 lunch receipt for EMP001",
    ),
]

# Default ceiling for the silent-empty rate. A demo that drops one turn in
# twenty is still visibly broken on stage, so this is deliberately tight.
DEFAULT_THRESHOLD = 0.05

OUTCOMES = ("FULL", "EMPTY", "EMPTY_LABELLED", "THROTTLED")

# A turn we never managed to send (session-create or transport failure). Not an
# outcome of the router, so it is excluded from every rate — but recorded and
# reported, because a half-finished run must not read as a complete one.
SKIPPED = "SKIPPED"


def _visible_text(events: Sequence[dict]) -> str:
    from src.traffic.generate_traffic import _extract_text

    return "".join(_extract_text(e) for e in events).strip()


def classify_outcome(events: Sequence[dict]) -> str:
    """Bucket one turn's events into one of :data:`OUTCOMES`.

    Text that starts with a retry-wrapper marker is a *labelled* failure, not a
    success — scoring it as FULL would hide exactly what the markers exist to
    reveal.
    """
    text = _visible_text(events)
    if not text:
        return "EMPTY"
    if text.startswith(THROTTLED_RESPONSE_PREFIX):
        return "THROTTLED"
    if text.startswith(EMPTY_RESPONSE_PREFIX):
        return "EMPTY_LABELLED"
    return "FULL"


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = pct * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (rank - lo)


def _rates(outcomes: list[str]) -> dict[str, Any]:
    n = len(outcomes)
    silent = sum(1 for o in outcomes if o == "EMPTY")
    full = sum(1 for o in outcomes if o == "FULL")
    return {
        "n": n,
        "silent_empty": silent,
        "labelled_failure": sum(1 for o in outcomes if o in ("EMPTY_LABELLED", "THROTTLED")),
        "empty_rate": (silent / n) if n else 0.0,
        "empty_rate_ci": wilson_ci(silent, n) if n else (0.0, 0.0),
        "full_rate": (full / n) if n else 0.0,
    }


def summarize(results: Sequence[dict]) -> dict[str, Any]:
    """Aggregate per-probe results into counts, rates (with CI) and latencies.

    ``empty_rate`` counts **silent** empties only — a labelled throttle/empty is
    a failed turn but not a silent one, and conflating them would make the retry
    wrapper's whole contribution invisible.

    :data:`SKIPPED` turns are excluded from every rate (they say nothing about
    the router) but reported as ``skipped`` so a truncated run is visible.
    """
    scored = [r for r in results if r["outcome"] != SKIPPED]
    outcomes = [r["outcome"] for r in scored]
    summary = _rates(outcomes)
    summary["counts"] = {o: outcomes.count(o) for o in OUTCOMES}
    summary["skipped"] = sum(1 for r in results if r["outcome"] == SKIPPED)

    ok_latencies = sorted(r["latency_s"] for r in scored if r["outcome"] == "FULL")
    summary["p50_latency_s"] = _percentile(ok_latencies, 0.50)
    summary["p95_latency_s"] = _percentile(ok_latencies, 0.95)

    by_tier: dict[str, list[str]] = {}
    for r in scored:
        by_tier.setdefault(r["tier"], []).append(r["outcome"])
    summary["by_tier"] = {tier: _rates(outs) for tier, outs in sorted(by_tier.items())}
    return summary


def verdict(summary: dict, *, threshold: float = DEFAULT_THRESHOLD) -> dict[str, Any]:
    """PASS/FAIL on the silent-empty rate. Zero samples never passes."""
    n = summary.get("n", 0)
    rate = summary.get("empty_rate", 0.0)
    return {
        "passed": bool(n) and rate <= threshold,
        "threshold": threshold,
        "reason": "no samples" if not n else f"silent empty rate {rate:.1%} vs {threshold:.1%}",
    }


def format_report(summary: dict, decision: dict) -> str:
    lines = [
        "=" * 60,
        "ROUTER HEALTH",
        "=" * 60,
        f"  turns:            {summary['n']}",
        f"  full:             {summary['counts']['FULL']} ({summary['full_rate']:.1%})",
        f"  silent empty:     {summary['silent_empty']} ({summary['empty_rate']:.1%}) "
        f"95% CI [{summary['empty_rate_ci'][0]:.1%}, {summary['empty_rate_ci'][1]:.1%}]",
        f"  labelled failure: {summary['labelled_failure']} "
        f"(throttled={summary['counts']['THROTTLED']}, "
        f"empty={summary['counts']['EMPTY_LABELLED']})",
        f"  latency:          p50 {summary['p50_latency_s']:.1f}s / "
        f"p95 {summary['p95_latency_s']:.1f}s",
        f"  skipped:          {summary.get('skipped', 0)} (not scored — session/transport)",
        "",
        "  by tier:",
    ]
    lines.extend(
        f"    {tier:6} n={s['n']:3} full={s['full_rate']:6.1%} empty={s['empty_rate']:6.1%}"
        for tier, s in summary.get("by_tier", {}).items()
    )
    lines += ["", f"  VERDICT: {'PASS' if decision['passed'] else 'FAIL'} ({decision['reason']})"]
    return "\n".join(lines)


def run_probes(
    resource: str,
    *,
    repeat: int = 1,
    user_id: str = "router-health",
    spacing_s: float = 4.0,
    probes: Sequence[tuple[str, str]] | None = None,
    stream_fn=None,
    session_fn=None,
    sleep=time.sleep,
    verbose: bool = True,
) -> list[dict]:
    """Send every probe ``repeat`` times and return one result row per turn.

    ``stream_fn``/``session_fn``/``sleep`` are injectable so the loop can be
    exercised without a live engine. A fresh session per turn is deliberate: a
    reused session would let one poisoned context explain later empties.
    """
    from src.eval.raw_stream import create_session, stream_query_events

    stream_fn = stream_fn or stream_query_events
    session_fn = session_fn or create_session
    probes = probes if probes is not None else PROBES

    results: list[dict] = []
    for rep in range(repeat):
        for tier, prompt in probes:

            def _skip(exc: Exception, *, tier=tier, prompt=prompt, rep=rep) -> None:
                if verbose:
                    print(f"  [{tier:6}#{rep}] {SKIPPED:14} {type(exc).__name__}: {exc}")
                results.append(
                    {
                        "tier": tier,
                        "prompt": prompt,
                        "outcome": SKIPPED,
                        "chars": 0,
                        "events": 0,
                        "latency_s": 0.0,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )

            # Neither a control-plane hiccup nor a transport reset is evidence
            # about the router, and neither may abort the remaining turns: the
            # first live run lost 11 of 28 measurements to one create_session
            # KeyError.
            try:
                session_id = session_fn(resource, user_id)
            except Exception as exc:
                _skip(exc)
                sleep(spacing_s)
                continue
            t0 = time.time()
            try:
                events = stream_fn(resource, message=prompt, user_id=user_id, session_id=session_id)
            except Exception as exc:
                _skip(exc)
                sleep(spacing_s)
                continue
            latency = time.time() - t0
            outcome = classify_outcome(events)
            results.append(
                {
                    "tier": tier,
                    "prompt": prompt,
                    "outcome": outcome,
                    "chars": len(_visible_text(events)),
                    "events": len(events),
                    "latency_s": latency,
                }
            )
            if verbose:
                print(
                    f"  [{tier:6}#{rep}] {outcome:14} {latency:5.1f}s "
                    f"events={len(events):2} chars={results[-1]['chars']}"
                )
            sleep(spacing_s)
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify router health / empty-stream rate")
    parser.add_argument("--agent-id", required=True, help="Router engine id or resource name")
    parser.add_argument("--repeat", type=int, default=2, help="Passes over the probe set")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD, help="Max silent-empty rate"
    )
    parser.add_argument("--spacing", type=float, default=4.0, help="Seconds between turns")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a report")
    args = parser.parse_args(argv)

    from src.eval.batch_eval import _resolve_agent_resource_name

    resource = _resolve_agent_resource_name(args.agent_id)
    if not args.json:
        print(f"Router: {resource}\n")

    results = run_probes(
        resource, repeat=args.repeat, spacing_s=args.spacing, verbose=not args.json
    )
    summary = summarize(results)
    decision = verdict(summary, threshold=args.threshold)

    if args.json:
        print(json.dumps({"summary": summary, "verdict": decision, "results": results}, indent=2))
    else:
        print("\n" + format_report(summary, decision))
    return 0 if decision["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
