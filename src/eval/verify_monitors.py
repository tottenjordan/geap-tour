"""Verify periodic-snapshot evaluation results across two monitoring surfaces.

The canonical source of truth is Cloud Monitoring. Two independent surfaces are
summarized because the coordinator and the router are architecturally different
agents:

* ``coordinator_quality`` — the ``agent_eval/*`` gauges (LLM rubrics on a 1-5
  axis; alert on the floor, ``LT``). The coordinator is a task executor: success
  is output quality. These are the periodic *offline* snapshot (one write per
  eval run, ``eval_mode=offline``).
* ``online_quality`` — the ``agent_online_eval/*`` gauges (same 1-5 rubric axis,
  same ``LT`` floor). These are *continuous* scores sampled off live traffic
  (``eval_mode=online``, published by ``src.eval.online_monitor``) — a separate
  family so the continuous online series never blurs with the offline snapshot.
* ``router_efficiency`` — the ``agent_router/*`` gauges (native units: routing
  accuracy %, cost savings %, classifier latency ms). The router is an economic
  optimizer: routing accuracy / cost savings alert on the floor (``LT``) and
  latency alerts on the ceiling (``GT``).

An OPTIONAL, guarded BigQuery export path remains for anyone who wires up their
own export sink (``source="bigquery"``); it degrades gracefully (status
``no_table``) when the table is absent rather than crashing. The BigQuery path
only covers the coordinator quality series.

Usage:
    uv run python -m src.eval.verify_monitors                    # Cloud Monitoring
    uv run python -m src.eval.verify_monitors --format json
    uv run python -m src.eval.verify_monitors --source bigquery  # optional export
"""

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from src.config import BQ_EVAL_DATASET, GCP_PROJECT_ID
from src.eval.quality_alerts import (
    ALL_MONITORED_METRICS,
    ONLINE_MONITORED_METRICS,
    ROUTER_MONITORED_METRICS,
)

DEFAULT_THRESHOLD = 3.0


class Surface(NamedTuple):
    """A monitored surface: metric-type prefix + per-metric (name, threshold, comparison).

    ``comparison`` is "LT" (out of bounds below the floor) or "GT" (above ceiling).
    """

    prefix: str
    metrics: list[tuple[str, float, str]]


# The two monitored surfaces the coordinator (quality) and router (efficiency) map to.
SURFACES = {
    "coordinator_quality": Surface(
        prefix="custom.googleapis.com/agent_eval/",
        metrics=[(name, threshold, "LT") for name, threshold in ALL_MONITORED_METRICS],
    ),
    "online_quality": Surface(
        prefix="custom.googleapis.com/agent_online_eval/",
        metrics=[(name, threshold, "LT") for name, threshold in ONLINE_MONITORED_METRICS],
    ),
    "router_efficiency": Surface(
        prefix="custom.googleapis.com/agent_router/",
        metrics=list(ROUTER_MONITORED_METRICS),
    ),
}


# --------------------------------------------------------------------------- #
# Canonical source: Cloud Monitoring surfaces
# --------------------------------------------------------------------------- #
def _monitoring_client():
    """Lazily construct a MetricServiceClient (import-safe without credentials)."""
    from google.cloud import monitoring_v3

    return monitoring_v3.MetricServiceClient()


def _point_epoch(point) -> float:
    """Epoch seconds for a TimeSeries point, tolerant of proto or fake shapes."""
    end = point.interval.end_time
    if hasattr(end, "timestamp"):
        return end.timestamp()
    if hasattr(end, "seconds"):
        return float(end.seconds)
    return float(end)


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = round((pct / 100.0) * (len(ordered) - 1))
    return round(ordered[k], 3)


def _query_surface_series(client, prefix: str, metric_specs, hours: int):
    """Yield the raw TimeSeries for a surface's metrics over the trailing window.

    ``list_time_series`` requires the filter to resolve to a *single* metric
    type — a ``starts_with`` prefix that matches more than one metric 400s — so
    each monitored metric is queried with an exact ``metric.type`` match and the
    results are chained.
    """
    from google.cloud import monitoring_v3

    now = datetime.now(tz=UTC)
    interval = monitoring_v3.TimeInterval(
        start_time=now - timedelta(hours=hours),
        end_time=now,
    )
    for name, _threshold, _comparison in metric_specs:
        request = {
            "name": f"projects/{GCP_PROJECT_ID}",
            "filter": f'metric.type = "{prefix}{name}"',
            "interval": interval,
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        }
        yield from client.list_time_series(request=request)


def _window_avg(
    scores: list[float], epochs: list[float], now: float, max_hours: float
) -> float | None:
    vals = [s for s, ep in zip(scores, epochs, strict=True) if (now - ep) / 3600.0 <= max_hours]
    return round(sum(vals) / len(vals), 3) if vals else None


def _out_of_bounds(scores: list[float], threshold: float, comparison: str) -> int:
    """Count points that violate the alert direction (LT: below; GT: above)."""
    if comparison == "GT":
        return sum(1 for s in scores if s > threshold)
    return sum(1 for s in scores if s < threshold)


def _summarize(
    scores: list[float], epochs: list[float], threshold: float, comparison: str, now: float
) -> dict:
    """Build the per-metric summary dict for one score/epoch bucket."""
    return {
        "eval_count": len(scores),
        "avg_score": round(sum(scores) / len(scores), 3),
        "min_score": round(min(scores), 3),
        "max_score": round(max(scores), 3),
        "p50_score": _percentile(scores, 50),
        "p90_score": _percentile(scores, 90),
        "threshold": threshold,
        "direction": comparison,
        "out_of_bounds": _out_of_bounds(scores, threshold, comparison),
        "first_eval": datetime.fromtimestamp(min(epochs), tz=UTC).isoformat(),
        "last_eval": datetime.fromtimestamp(max(epochs), tz=UTC).isoformat(),
        "trend": {
            "avg_1h": _window_avg(scores, epochs, now, 1),
            "avg_6h": _window_avg(scores, epochs, now, 6),
            "avg_24h": _window_avg(scores, epochs, now, 24),
        },
    }


def _aggregate_surface(
    series_iter, metric_specs, now: float | None = None, group_by_label: str | None = None
) -> dict:
    """Collapse a surface's TimeSeries into the per-metric summary dict shape.

    Ungrouped (default), ``metrics[name]`` is a flat summary. When
    ``group_by_label`` is set (e.g. ``"model"``), buckets are keyed by
    ``(metric_name, label_value)`` so two deployments render as separate series
    rather than a merged average: ``metrics[name][label_value]`` is the summary
    and the surface carries a ``group_by`` marker.
    """
    now = now if now is not None else time.time()
    directions = {name: (threshold, comparison) for name, threshold, comparison in metric_specs}

    # Bucket key is always ``(metric_name, label_value)``. Ungrouped runs use a
    # single ``""`` label so the same code path serves both shapes.
    buckets: dict[tuple[str, str], dict[str, list[float]]] = {}
    for series in series_iter:
        name = series.metric.type.rsplit("/", 1)[-1]
        if group_by_label is None:
            label_value = ""
        else:
            label_value = (getattr(series.metric, "labels", {}) or {}).get(
                group_by_label, "unknown"
            )
        bucket = buckets.setdefault((name, label_value), {"scores": [], "epochs": []})
        for point in series.points:
            bucket["scores"].append(float(point.value.double_value))
            bucket["epochs"].append(_point_epoch(point))

    metrics: dict[str, dict] = {}
    total = 0
    for (name, label_value), bucket in sorted(buckets.items()):
        scores = bucket["scores"]
        if not scores:
            continue
        total += len(scores)
        threshold, comparison = directions.get(name, (DEFAULT_THRESHOLD, "LT"))
        summary = _summarize(scores, bucket["epochs"], threshold, comparison, now)
        if group_by_label is None:
            metrics[name] = summary
        else:
            metrics.setdefault(name, {})[label_value] = summary

    surface = {"status": "ok" if metrics else "empty", "metrics": metrics, "total_evals": total}
    if group_by_label is not None:
        surface["group_by"] = group_by_label
    return surface


def _verify_from_monitoring(hours: int, client=None, group_by: str | None = None) -> dict:
    client = client or _monitoring_client()
    data: dict[str, object] = {}
    any_ok = False
    for surface_key, spec in SURFACES.items():
        series = list(_query_surface_series(client, spec.prefix, spec.metrics, hours))
        surface = _aggregate_surface(series, spec.metrics, group_by_label=group_by)
        data[surface_key] = surface
        any_ok = any_ok or surface["status"] == "ok"
    data["status"] = "ok" if any_ok else "empty"
    return data


# --------------------------------------------------------------------------- #
# Optional export sink: BigQuery (guarded — never the primary source)
# --------------------------------------------------------------------------- #
def _bq_table_ref() -> str:
    return f"{GCP_PROJECT_ID}.{BQ_EVAL_DATASET}.online_eval_results"


def _verify_from_bigquery(hours: int, threshold: float, bq_client=None) -> dict:
    if bq_client is None:
        from google.cloud import bigquery

        bq_client = bigquery.Client(project=GCP_PROJECT_ID)

    table_ref = _bq_table_ref()
    try:
        bq_client.get_table(table_ref)
    except Exception:
        return {
            "status": "no_table",
            "message": (
                f"Optional BigQuery export table {table_ref} does not exist. This is "
                "an optional sink — the canonical source is Cloud Monitoring "
                "(run without --source bigquery)."
            ),
        }

    query = f"""
    SELECT metric_name,
           COUNT(*) as eval_count,
           ROUND(AVG(score), 3) as avg_score,
           ROUND(MIN(score), 3) as min_score,
           ROUND(MAX(score), 3) as max_score,
           COUNTIF(score < {threshold}) as below_threshold
    FROM `{table_ref}`
    WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {hours} HOUR)
    GROUP BY metric_name
    ORDER BY metric_name
    """
    try:
        rows = list(bq_client.query(query).result())
    except Exception as e:
        return {"status": "error", "error": str(e)}

    if not rows:
        return {"status": "empty", "message": "No rows in the export table for the window."}

    metrics = {
        row.metric_name: {
            "eval_count": row.eval_count,
            "avg_score": row.avg_score,
            "min_score": row.min_score,
            "max_score": row.max_score,
            "p50_score": None,
            "p90_score": None,
            "out_of_bounds": row.below_threshold,
            "direction": "LT",
            "trend": {"avg_1h": None, "avg_6h": None, "avg_24h": row.avg_score},
        }
        for row in rows
    }
    return {
        "status": "ok",
        "metrics": metrics,
        "total_evals": sum(r.eval_count for r in rows),
    }


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def verify_monitor_results(
    output_format: str = "text",
    source: str = "monitoring",
    hours: int = 24,
    threshold: float = DEFAULT_THRESHOLD,
    client=None,
    bq_client=None,
    group_by: str | None = None,
) -> dict | None:
    """Summarize periodic-snapshot quality/efficiency scores.

    Args:
        output_format: ``"text"`` (human-readable) or ``"json"`` (return dict).
        source: ``"monitoring"`` (canonical Cloud Monitoring surfaces) or
            ``"bigquery"`` (optional, guarded export sink — coordinator only).
        hours: trailing window.
        threshold: below-this counts out-of-bounds for the BigQuery path.
        client / bq_client: injectable clients for tests.
        group_by: optional metric-label name (e.g. ``"model"``) to split each
            metric into per-label buckets, so two deployments render separately
            instead of collapsing into a merged average (monitoring path only).

    Returns:
        dict with results when ``output_format == "json"``, else ``None``. The
        monitoring path returns three surface blocks (``coordinator_quality``,
        ``online_quality``, ``router_efficiency``) plus a top-level ``status``.
    """
    if source == "bigquery":
        data = _verify_from_bigquery(hours, threshold, bq_client=bq_client)
    else:
        data = _verify_from_monitoring(hours, client=client, group_by=group_by)

    if output_format == "json":
        return data

    _print_report(data, hours)
    return None


_SURFACE_TITLES = {
    "coordinator_quality": "COORDINATOR QUALITY (agent_eval/*, 1-5 rubric, offline snapshot)",
    "online_quality": "ONLINE QUALITY (agent_online_eval/*, 1-5 rubric, live sampled)",
    "router_efficiency": "ROUTER EFFICIENCY (agent_router/*, native units)",
}


def _print_metric(m: dict, indent: str = "  ") -> None:
    op = ">" if m.get("direction") == "GT" else "<"
    print(f"{indent}  Evals:  {m['eval_count']}")
    print(f"{indent}  Avg:    {m['avg_score']}  (min: {m['min_score']}, max: {m['max_score']})")
    print(f"{indent}  P50:    {m['p50_score']}  P90: {m['p90_score']}")
    trend = m["trend"]
    parts = []
    if trend.get("avg_1h") is not None:
        parts.append(f"1h: {trend['avg_1h']}")
    if trend.get("avg_6h") is not None:
        parts.append(f"6h: {trend['avg_6h']}")
    parts.append(f"24h: {trend.get('avg_24h')}")
    print(f"{indent}  Trend:  {' | '.join(parts)}")
    if m["out_of_bounds"]:
        print(f"{indent}  WARNING: {m['out_of_bounds']} scores {op} {m.get('threshold')}")


def _print_surface(title: str, surface: dict) -> None:
    print(f"\n{title}")
    print("-" * 60)
    if surface.get("status") != "ok":
        print(f"  {surface.get('message', 'No scores in Cloud Monitoring yet.')}")
        return
    grouped = surface.get("group_by")
    print(f"  Total evaluations: {surface['total_evals']}\n")
    for metric_name, m in surface["metrics"].items():
        if grouped:
            print(f"  {metric_name} (by {grouped}):")
            for label_value, sub in m.items():
                print(f"    [{grouped}={label_value}]")
                _print_metric(sub, indent="    ")
                print()
        else:
            print(f"  {metric_name}:")
            _print_metric(m)
            print()


def _print_report(data: dict, hours: int) -> None:
    # BigQuery / non-surfaced shapes: single message.
    if "coordinator_quality" not in data and data.get("status") != "ok":
        print(data.get("message", data.get("error", "Unknown status")))
        return

    print("=" * 60)
    print(f"MONITOR RESULTS (last {hours}h)")
    print("=" * 60)
    for surface_key, title in _SURFACE_TITLES.items():
        if surface_key in data:
            _print_surface(title, data[surface_key])
    print("=" * 60)


def generate_markdown_report(data: dict) -> str:
    """Generate a markdown summary report from verify results (both surfaces)."""
    if "coordinator_quality" not in data and data.get("status") != "ok":
        return f"## Monitor Status\n\n{data.get('message', data.get('error', 'Unknown'))}\n"

    lines = ["## Monitor Health Report", ""]
    for surface_key, title in _SURFACE_TITLES.items():
        surface = data.get(surface_key)
        if not surface:
            continue
        lines += ["", f"### {title}", ""]
        if surface.get("status") != "ok":
            lines.append(surface.get("message", "No scores in Cloud Monitoring yet."))
            continue
        grouped = surface.get("group_by")

        def _row(name: str, m: dict, label: str | None = None) -> str:
            trend_1h = f"{m['trend']['avg_1h']}" if m["trend"].get("avg_1h") is not None else "N/A"
            op = ">" if m.get("direction") == "GT" else "<"
            lead = f"| {name} | {label} |" if label is not None else f"| {name} |"
            return (
                f"{lead} {m['eval_count']} | {m['avg_score']} | "
                f"{m['p50_score']} | {m['p90_score']} | {m['out_of_bounds']} | "
                f"{op} {m.get('threshold')} | {trend_1h} |"
            )

        lines += [f"**Total evaluations:** {surface['total_evals']}", ""]
        if grouped:
            lines += [
                f"| Metric | {grouped.capitalize()} | Evals | Avg | P50 | P90 | "
                "Out of bounds | Alert | 1h Trend |",
                "|--------|------|-------|-----|-----|-----|---------------|-------|----------|",
            ]
            for name, by_label in surface["metrics"].items():
                for label_value, m in by_label.items():
                    lines.append(_row(name, m, label=label_value))
        else:
            lines += [
                "| Metric | Evals | Avg | P50 | P90 | Out of bounds | Alert | 1h Trend |",
                "|--------|-------|-----|-----|-----|---------------|-------|----------|",
            ]
            for name, m in surface["metrics"].items():
                lines.append(_row(name, m))
    return "\n".join(lines)


if __name__ == "__main__":
    fmt = "text"
    if "--format" in sys.argv:
        idx = sys.argv.index("--format")
        if idx + 1 < len(sys.argv):
            fmt = sys.argv[idx + 1]

    src = "monitoring"
    if "--source" in sys.argv:
        idx = sys.argv.index("--source")
        if idx + 1 < len(sys.argv):
            src = sys.argv[idx + 1]

    group_by = None
    if "--group-by" in sys.argv:
        idx = sys.argv.index("--group-by")
        if idx + 1 < len(sys.argv):
            group_by = sys.argv[idx + 1]

    result = verify_monitor_results(output_format=fmt, source=src, group_by=group_by)
    if fmt == "json" and result:
        print(json.dumps(result, indent=2, default=str))
