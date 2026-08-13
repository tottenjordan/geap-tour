"""Custom Cloud Monitoring metrics writer.

Writes ``custom.googleapis.com/*`` TimeSeries so the alert policies created by
``src/eval/quality_alerts.py`` have something to fire on, and so the
dashboard-as-code in ``src/observability/dashboard.py`` has live data to render.

Two metric families are emitted:

* ``agent_traffic/*`` — operational signals from a synthetic load run
  (``generate_load`` summary): request latency (p50/p95), error rate, achieved
  QPS, and injected (hostile-query) count.
* ``agent_eval/*`` — evaluation quality scores. These metric types are the
  EXACT ones ``quality_alerts.py`` alerts on (imported from
  ``ALL_MONITORED_METRICS``), so the existing alert policies fire on the same
  series.

Design notes:
* The ``MetricServiceClient`` is constructed lazily on first write and stored on
  ``self._client``, so importing this module and constructing a ``MetricsWriter``
  needs no credentials — tests inject a fake client.
* Resource type is ``global`` (labels: ``project_id``) to match the
  ``resource.type="global"`` filter used by every policy in
  ``quality_alerts.py``. The agent engine id / region are carried as *metric*
  labels rather than resource labels so the alert series line up exactly.
* We emit plain GAUGE doubles rather than a real Distribution. Latency p50/p95
  are written as two separate gauges (``request_latency_p50`` /
  ``request_latency_p95``). A true Distribution would need bucket boundaries and
  a DELTA/CUMULATIVE metric kind, which is more machinery than a demo dashboard
  needs; two gauges chart identically and keep the writer trivial to test.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from google.api import metric_pb2, monitored_resource_pb2
from google.cloud import monitoring_v3

from src.config import AGENT_ENGINE_ID, GCP_PROJECT_ID, GCP_REGION, RESOURCE_LABELS
from src.eval.quality_alerts import ALL_MONITORED_METRICS, ROUTER_MONITORED_METRICS

if TYPE_CHECKING:
    from collections.abc import Mapping

METRIC_PREFIX = "custom.googleapis.com/"


def parse_labels(pairs) -> dict[str, str]:
    """Parse repeatable ``--label KEY=VALUE`` CLI pairs into a label dict.

    Shared by the traffic and eval-publish CLIs so a bake-off can stamp a
    ``model=…`` label on every emitted series (traffic + offline snapshots),
    keeping two deployments as separate monitoring series. ``None``/empty ->
    ``{}``. A pair without ``=`` is a user error and raises ``ValueError`` (fail
    loud rather than silently drop a monitoring label).
    """
    out: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ValueError(f"--label must be KEY=VALUE, got: {pair!r}")
        key, value = pair.split("=", 1)
        out[key] = value
    return out

# Quality metric types, derived from the SAME source quality_alerts.py alerts on.
QUALITY_METRIC_TYPES = [
    f"{METRIC_PREFIX}agent_eval/{name}" for name, _threshold in ALL_MONITORED_METRICS
]

# Router efficiency metric types (native units), from the SAME source the router
# alert policies read.
ROUTER_METRIC_TYPES = [
    f"{METRIC_PREFIX}agent_router/{name}"
    for name, _threshold, _comparison in ROUTER_MONITORED_METRICS
]

# Traffic metric types (bare, un-prefixed) emitted from a load-run summary.
TRAFFIC_LATENCY_P50 = "agent_traffic/request_latency_p50"
TRAFFIC_LATENCY_P95 = "agent_traffic/request_latency_p95"
TRAFFIC_ERROR_RATE = "agent_traffic/error_rate"
TRAFFIC_QPS = "agent_traffic/qps"
TRAFFIC_INJECTED = "agent_traffic/injected"

TRAFFIC_METRIC_TYPES = [
    f"{METRIC_PREFIX}{m}"
    for m in (
        TRAFFIC_LATENCY_P50,
        TRAFFIC_LATENCY_P95,
        TRAFFIC_ERROR_RATE,
        TRAFFIC_QPS,
        TRAFFIC_INJECTED,
    )
]


def _normalize_metric_type(metric_type: str) -> str:
    """Return a fully-prefixed metric type, accepting bare or already-prefixed."""
    if metric_type.startswith(METRIC_PREFIX):
        return metric_type
    return f"{METRIC_PREFIX}{metric_type.lstrip('/')}"


class MetricsWriter:
    """Thin, testable wrapper over ``monitoring_v3.MetricServiceClient``."""

    def __init__(self, project_id: str = GCP_PROJECT_ID, client=None):
        self.project_id = project_id
        self._client = client

    @property
    def client(self):
        """Lazily construct the metric client (only on first real write)."""
        if self._client is None:
            self._client = monitoring_v3.MetricServiceClient()
        return self._client

    @property
    def project_path(self) -> str:
        return f"projects/{self.project_id}"

    def write_gauge(
        self,
        metric_type: str,
        value: float,
        labels: Mapping[str, str] | None = None,
        resource_labels: Mapping[str, str] | None = None,
        monotonic: Callable[[], float] = time.time,
    ) -> None:
        """Write a single-point GAUGE double to ``metric_type``.

        ``metric_type`` may be bare (``agent_traffic/qps``) or already prefixed
        (``custom.googleapis.com/...``); it is normalized either way. The point
        is stamped with ``monotonic()`` (epoch seconds; injectable for tests).
        """
        now = monotonic()
        seconds = int(now)
        nanos = int((now - seconds) * 1e9)
        interval = monitoring_v3.TimeInterval(end_time={"seconds": seconds, "nanos": nanos})
        point = monitoring_v3.Point(
            interval=interval,
            value=monitoring_v3.TypedValue(double_value=float(value)),
        )

        series = monitoring_v3.TimeSeries()
        series.metric = metric_pb2.Metric(
            type=_normalize_metric_type(metric_type),
            labels=dict(labels or {}),
        )
        series.resource = monitored_resource_pb2.MonitoredResource(
            type="global",
            labels={"project_id": self.project_id, **dict(resource_labels or {})},
        )
        series.metric_kind = metric_pb2.MetricDescriptor.MetricKind.GAUGE
        series.value_type = metric_pb2.MetricDescriptor.ValueType.DOUBLE
        series.points = [point]

        self.client.create_time_series(name=self.project_path, time_series=[series])


def _default_labels(extra_labels: Mapping[str, str] | None) -> dict[str, str]:
    """Base metric labels (engine + region) plus any caller-supplied extras."""
    labels = {"engine_id": AGENT_ENGINE_ID, "region": GCP_REGION, **RESOURCE_LABELS}
    if extra_labels:
        labels.update(extra_labels)
    return labels


def emit_traffic_metrics(
    summary: Mapping[str, float],
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
) -> None:
    """Emit ``agent_traffic/*`` gauges from a ``generate_load`` summary dict.

    Expected keys: ``offered, sent, errors, injected, achieved_qps,
    p50_latency, p95_latency, duration_s``.
    """
    writer = writer or MetricsWriter()
    labels = _default_labels(extra_labels)

    offered = summary.get("offered", 0)
    error_rate = summary.get("errors", 0) / max(offered, 1)

    writer.write_gauge(TRAFFIC_LATENCY_P50, summary.get("p50_latency", 0.0), labels)
    writer.write_gauge(TRAFFIC_LATENCY_P95, summary.get("p95_latency", 0.0), labels)
    writer.write_gauge(TRAFFIC_ERROR_RATE, error_rate, labels)
    writer.write_gauge(TRAFFIC_QPS, summary.get("achieved_qps", 0.0), labels)
    writer.write_gauge(TRAFFIC_INJECTED, summary.get("injected", 0), labels)


def write_quality_scores(
    scores: Mapping[str, float],
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
) -> None:
    """Emit ``agent_eval/<name>`` gauges for evaluation scores.

    Keys are the bare metric names (e.g. ``helpfulness``) matching
    ``quality_alerts.ALL_MONITORED_METRICS``.
    """
    writer = writer or MetricsWriter()
    labels = _default_labels(extra_labels)
    for name, value in scores.items():
        writer.write_gauge(f"agent_eval/{name}", value, labels)


def write_router_metrics(
    scores: Mapping[str, float],
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
) -> None:
    """Emit ``agent_router/<name>`` gauges for router efficiency scores.

    Keys are the bare metric names (e.g. ``cost_savings_pct``) matching
    ``quality_alerts.ROUTER_MONITORED_METRICS``. Values are written verbatim in
    native units (percent, ms) — unlike ``write_quality_scores`` there is no
    0-1 -> 1-5 scaling, because the router is an economic optimizer, not a
    quality-rubric surface.
    """
    writer = writer or MetricsWriter()
    labels = _default_labels(extra_labels)
    for name, value in scores.items():
        writer.write_gauge(f"agent_router/{name}", value, labels)
