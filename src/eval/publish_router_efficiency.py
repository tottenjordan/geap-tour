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
) -> dict[str, float]:
    """Publish router efficiency scores to ``agent_router/*``; return what was written.

    ``accuracy_results`` is a :func:`run_complexity_accuracy_eval` result
    (``{"accuracy": 0-1, "avg_latency_ms": ms}``); ``cost_results`` is a
    :func:`run_cost_efficiency_eval` result (``{"savings_pct": 0-100}``). Missing
    inputs or keys are skipped (never zeroed), so a partial run publishes only
    what it has. Values are native units — no 0-1 -> 1-5 scaling.
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
        "--dry-run",
        action="store_true",
        help="compute and print scores without writing to Cloud Monitoring",
    )
    args = parser.parse_args(argv)

    accuracy, cost = _load_results(args.from_json)

    writer = None
    if args.dry_run:
        writer = MetricsWriter(client=_NoopMetricClient())

    published = publish_router_efficiency(accuracy, cost, writer=writer)
    prefix = "[dry-run] would publish" if args.dry_run else "published"
    print(f"{prefix}: {json.dumps(published, indent=2, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
