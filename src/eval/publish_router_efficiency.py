"""Bridge router-efficiency scores onto the ``agent_router/*`` monitoring series.

The 5-tier router is an *economic optimizer*, not a task executor: success is
routing accuracy + cost savings vs an all-Opus baseline + classifier latency —
NOT response quality. So its numbers live on their own series in native units
(percent, ms), separate from the coordinator's 1-5 quality axis on
``agent_eval/*``.

``run_complexity_accuracy_eval`` and ``run_cost_efficiency_eval`` (both local
classifier evals, no engine call) already compute these numbers; this module
extracts the three monitored router metrics and hands them to
:func:`src.observability.metrics.write_router_metrics` verbatim (no scaling).
Every point carries ``eval_mode="offline"`` — these are periodic snapshots per
eval run, not per-request telemetry.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

from src.observability.metrics import MetricsWriter, write_router_metrics

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def publish_router_efficiency(
    accuracy_results: Mapping | None,
    cost_results: Mapping | None,
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
    experiment_name: str | None = None,
    experiment_run: str = "offline",
    log_run_fn=None,
) -> dict[str, float]:
    """Publish router efficiency scores to ``agent_router/*``; return what was written.

    ``accuracy_results`` is a :func:`run_complexity_accuracy_eval` result
    (``{"accuracy": 0-1, "avg_latency_ms": ms}``); ``cost_results`` is a
    :func:`run_cost_efficiency_eval` result (``{"savings_pct": 0-100}``). Missing
    inputs or keys are skipped (never zeroed), so a partial run publishes only
    what it has. Values are native units — no 0-1 -> 1-5 scaling.

    When ``experiment_name`` is set, the same native-unit scores are also recorded
    as one Vertex AI Experiments run (best-effort) into the router's **own**
    experiment — kept strictly separate from the coordinator's ``coordinator-bakeoff``
    series. Dormant by default (no experiment name → no Vertex resource).
    """
    accuracy_results = accuracy_results or {}
    cost_results = cost_results or {}

    scores: dict[str, float] = {}
    accuracy = accuracy_results.get("accuracy")
    if accuracy is not None:
        scores["routing_accuracy_pct"] = round(float(accuracy) * 100.0, 1)
    savings = cost_results.get("savings_pct")
    if savings is not None:
        scores["cost_savings_pct"] = round(float(savings), 1)
    latency = accuracy_results.get("avg_latency_ms")
    if latency is not None:
        scores["classifier_latency_ms"] = round(float(latency), 1)

    if not scores:
        return {}

    labels = {"eval_mode": "offline", **(extra_labels or {})}
    write_router_metrics(scores, writer=writer, extra_labels=labels)

    # Optional durable record — the router's economic-optimizer metrics in their
    # own experiment, never mixed with the coordinator's quality axis. Best-effort.
    if log_run_fn is None:
        from src.observability.experiments import log_run as log_run_fn
    log_run_fn(
        experiment=experiment_name,
        run=experiment_run,
        params={"surface": "router"},
        metrics=scores,
    )
    return scores


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class _NoopMetricClient:
    """Swallow ``create_time_series`` — used for ``--dry-run`` (no GCP)."""

    def create_time_series(self, name=None, time_series=None):
        return None


def _load_results(path: str) -> tuple[dict, dict]:
    """Load a ``run_all_evals`` full_results.json → ``(accuracy, cost)`` blocks."""
    with open(path) as f:
        data = json.load(f)
    complexity = data.get("complexity", data)
    return complexity.get("accuracy", {}), complexity.get("cost_efficiency", {})


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the router-efficiency → ``agent_router/*`` bridge."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--from-json",
        metavar="PATH",
        required=True,
        help="load a run_all_evals full_results.json (reads complexity.accuracy / .cost_efficiency)",
    )
    parser.add_argument(
        "--label",
        action="append",
        metavar="KEY=VALUE",
        help="extra label stamped on every published series (repeatable; e.g. "
        "--label model=gemini-3.6-flash keeps a bake-off's snapshots separable)",
    )
    parser.add_argument(
        "--experiment-name",
        default="",
        help="also record one Vertex Experiments run into this experiment "
        "(e.g. router-efficiency, kept separate from coordinator-bakeoff); off by default",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute and print scores without writing to Cloud Monitoring",
    )
    args = parser.parse_args(argv)

    accuracy, cost = _load_results(args.from_json)

    writer = None
    log_run_fn = None
    if args.dry_run:
        writer = MetricsWriter(client=_NoopMetricClient())
        log_run_fn = lambda **_k: False  # noqa: E731 — no Vertex writes in a dry run

    from src.observability.metrics import parse_labels

    published = publish_router_efficiency(
        accuracy,
        cost,
        writer=writer,
        extra_labels=parse_labels(args.label),
        experiment_name=args.experiment_name or None,
        log_run_fn=log_run_fn,
    )
    prefix = "[dry-run] would publish" if args.dry_run else "published"
    print(f"{prefix}: {json.dumps(published, indent=2, sort_keys=True)}")
    # routing_accuracy_pct is a proportion over the router eval cases, and its 80%
    # alert is only meaningful if that sample can resolve it. Say which it is here,
    # where the number is produced, rather than leaving it to the reader.
    _print_accuracy_power(published, accuracy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def _print_accuracy_power(published: dict, accuracy_results) -> None:
    """Note whether routing_accuracy_pct's sample can resolve its own 80% alert.

    At the historical n=12 it could not: the Wilson interval spanned 80% for every
    possible outcome, so a perfect score was indistinguishable from a failing one.
    """
    accuracy = published.get("routing_accuracy_pct")
    if accuracy is None:
        return
    total = (accuracy_results or {}).get("total_cases")
    if not total:
        return
    from src.eval.quality_alerts import ROUTER_MONITORED_METRICS
    from src.eval.stats import power_report

    floor = next(
        (t for name, t, _c in ROUTER_MONITORED_METRICS if name == "routing_accuracy_pct"), 80.0
    )
    report = power_report(round(accuracy / 100.0 * total), total, floor / 100.0)
    lo, hi = report["ci"]
    if report["resolved"]:
        print(
            f"  routing_accuracy: n={total}, 95% CI [{lo:.0%}, {hi:.0%}] — resolves the {floor:.0f}% alert"
        )
    else:
        needed = report["needed_n"]
        hint = f"~{needed} cases would" if needed else "no sample size will"
        print(
            f"  routing_accuracy: n={total}, 95% CI [{lo:.0%}, {hi:.0%}] — CANNOT resolve the "
            f"{floor:.0f}% alert ({hint} settle it)"
        )
