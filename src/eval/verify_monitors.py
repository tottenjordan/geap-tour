"""Verify continuous online-evaluation results.

The canonical source of truth is Cloud Monitoring: the ``agent_eval/*`` gauge
series that ``src/eval/publish_eval_metrics.py`` bridges native-evaluator scores
onto (the same series ``quality_alerts.py`` alerts on and the dashboard charts).
This module reads that series and summarizes it — it no longer requires the
BigQuery ``online_eval_results`` table, which nothing in the repo creates.

An OPTIONAL, guarded BigQuery export path remains for anyone who wires up their
own export sink (``source="bigquery"``); it degrades gracefully (status
``no_table``) when the table is absent rather than crashing.

Usage:
    uv run python -m src.eval.verify_monitors                    # Cloud Monitoring
    uv run python -m src.eval.verify_monitors --format json
    uv run python -m src.eval.verify_monitors --source bigquery  # optional export
"""

import json
import sys
import time
from datetime import UTC, datetime, timedelta

from src.config import BQ_EVAL_DATASET, GCP_PROJECT_ID

METRIC_PREFIX = "custom.googleapis.com/agent_eval/"
DEFAULT_THRESHOLD = 3.0


# --------------------------------------------------------------------------- #
# Canonical source: Cloud Monitoring agent_eval/* series
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


def _query_monitoring_series(client, hours: int):
    """Return the raw ``agent_eval/*`` TimeSeries over the trailing window."""
    from google.cloud import monitoring_v3

    now = datetime.now(tz=UTC)
    interval = monitoring_v3.TimeInterval(
        start_time=now - timedelta(hours=hours),
        end_time=now,
    )
    request = {
        "name": f"projects/{GCP_PROJECT_ID}",
        "filter": f'metric.type = starts_with("{METRIC_PREFIX}")',
        "interval": interval,
        "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
    }
    return client.list_time_series(request=request)


def _window_avg(scores: list[float], epochs: list[float], now: float, max_hours: float) -> float | None:
    vals = [
        s
        for s, ep in zip(scores, epochs, strict=True)
        if (now - ep) / 3600.0 <= max_hours
    ]
    return round(sum(vals) / len(vals), 3) if vals else None


def _aggregate_series(series_iter, threshold: float, now: float | None = None) -> dict:
    """Collapse TimeSeries points into the per-metric summary dict shape."""
    now = now if now is not None else time.time()
    buckets: dict[str, dict[str, list[float]]] = {}
    for series in series_iter:
        mtype = series.metric.type
        name = mtype.rsplit("/", 1)[-1]
        bucket = buckets.setdefault(name, {"scores": [], "epochs": []})
        for point in series.points:
            bucket["scores"].append(float(point.value.double_value))
            bucket["epochs"].append(_point_epoch(point))

    metrics: dict[str, dict] = {}
    total = 0
    for name, bucket in sorted(buckets.items()):
        scores = bucket["scores"]
        epochs = bucket["epochs"]
        if not scores:
            continue
        total += len(scores)

        first_epoch = min(bucket["epochs"])
        last_epoch = max(bucket["epochs"])
        metrics[name] = {
            "eval_count": len(scores),
            "avg_score": round(sum(scores) / len(scores), 3),
            "min_score": round(min(scores), 3),
            "max_score": round(max(scores), 3),
            "p50_score": _percentile(scores, 50),
            "p90_score": _percentile(scores, 90),
            "below_threshold": sum(1 for s in scores if s < threshold),
            "first_eval": datetime.fromtimestamp(first_epoch, tz=UTC).isoformat(),
            "last_eval": datetime.fromtimestamp(last_epoch, tz=UTC).isoformat(),
            "trend": {
                "avg_1h": _window_avg(scores, epochs, now, 1),
                "avg_6h": _window_avg(scores, epochs, now, 6),
                "avg_24h": _window_avg(scores, epochs, now, 24),
            },
        }

    return {"status": "ok", "metrics": metrics, "total_evals": total}


def _verify_from_monitoring(hours: int, threshold: float, client=None) -> dict:
    client = client or _monitoring_client()
    series = list(_query_monitoring_series(client, hours))
    data = _aggregate_series(series, threshold)
    if not data["metrics"]:
        return {
            "status": "empty",
            "message": (
                "No agent_eval/* scores in Cloud Monitoring yet. Ensure an online "
                "evaluator is running (src.eval.setup_online_evaluators create) and "
                "scores are bridged (src.eval.publish_eval_metrics)."
            ),
        }
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
            "below_threshold": row.below_threshold,
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
) -> dict | None:
    """Summarize online-evaluation quality scores.

    Args:
        output_format: ``"text"`` (human-readable) or ``"json"`` (return dict).
        source: ``"monitoring"`` (canonical Cloud Monitoring series) or
            ``"bigquery"`` (optional, guarded export sink).
        hours: trailing window.
        threshold: below-this counts toward ``below_threshold``.
        client / bq_client: injectable clients for tests.

    Returns:
        dict with results when ``output_format == "json"``, else ``None``.
    """
    if source == "bigquery":
        data = _verify_from_bigquery(hours, threshold, bq_client=bq_client)
    else:
        data = _verify_from_monitoring(hours, threshold, client=client)

    if output_format == "json":
        return data

    _print_report(data, hours)
    return None


def _print_report(data: dict, hours: int) -> None:
    if data.get("status") != "ok":
        print(data.get("message", data.get("error", "Unknown status")))
        return

    print("=" * 60)
    print(f"ONLINE MONITOR RESULTS (last {hours}h)")
    print("=" * 60)
    print(f"  Total evaluations: {data['total_evals']}\n")

    for metric_name, m in data["metrics"].items():
        print(f"  {metric_name}:")
        print(f"    Evals:  {m['eval_count']}")
        print(f"    Avg:    {m['avg_score']}  (min: {m['min_score']}, max: {m['max_score']})")
        print(f"    P50:    {m['p50_score']}  P90: {m['p90_score']}")
        trend = m["trend"]
        parts = []
        if trend.get("avg_1h") is not None:
            parts.append(f"1h: {trend['avg_1h']}")
        if trend.get("avg_6h") is not None:
            parts.append(f"6h: {trend['avg_6h']}")
        parts.append(f"24h: {trend.get('avg_24h')}")
        print(f"    Trend:  {' | '.join(parts)}")
        if m["below_threshold"]:
            print(f"    WARNING: {m['below_threshold']} scores below {DEFAULT_THRESHOLD}")
        print()
    print("=" * 60)


def generate_markdown_report(data: dict) -> str:
    """Generate a markdown summary report from verify results."""
    if data.get("status") != "ok":
        return f"## Monitor Status\n\n{data.get('message', data.get('error', 'Unknown'))}\n"

    lines = [
        "## Online Monitor Health Report",
        "",
        f"**Total evaluations (24h):** {data['total_evals']}",
        "",
        "| Metric | Evals | Avg | P50 | P90 | Below 3.0 | 1h Trend |",
        "|--------|-------|-----|-----|-----|-----------|----------|",
    ]
    for name, m in data["metrics"].items():
        trend_1h = f"{m['trend']['avg_1h']}" if m["trend"].get("avg_1h") is not None else "N/A"
        lines.append(
            f"| {name} | {m['eval_count']} | {m['avg_score']} | "
            f"{m['p50_score']} | {m['p90_score']} | {m['below_threshold']} | {trend_1h} |"
        )
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

    result = verify_monitor_results(output_format=fmt, source=src)
    if fmt == "json" and result:
        print(json.dumps(result, indent=2, default=str))
