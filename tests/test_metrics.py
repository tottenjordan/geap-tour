"""Offline tests for the custom Cloud Monitoring metrics writer (no live GCP).

A fake MetricServiceClient records every ``create_time_series`` call so we can
assert on the emitted metric types, points, and resource — without credentials.
"""

from src.eval.quality_alerts import ALL_MONITORED_METRICS
from src.observability import metrics
from src.observability.metrics import (
    QUALITY_METRIC_TYPES,
    MetricsWriter,
    emit_traffic_metrics,
)


class FakeMetricClient:
    """Records create_time_series calls; no network."""

    def __init__(self):
        self.calls = []  # list of (name, [TimeSeries])

    def create_time_series(self, name=None, time_series=None):
        self.calls.append((name, list(time_series)))

    def flatten(self):
        """Return all emitted TimeSeries across every call."""
        out = []
        for _name, ts_list in self.calls:
            out.extend(ts_list)
        return out


def _by_type(client):
    """Map metric.type -> single Point value for all emitted series."""
    result = {}
    for ts in client.flatten():
        result[ts.metric.type] = ts.points[0].value.double_value
    return result


def test_import_needs_no_credentials():
    """Importing and constructing a writer must not touch GCP."""
    w = MetricsWriter(project_id="proj-x")
    assert w._client is None  # client is lazy


def test_write_gauge_normalizes_metric_type():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    w.write_gauge("agent_traffic/qps", 12.5, monotonic=lambda: 1000.0)

    assert len(client.calls) == 1
    name, ts_list = client.calls[0]
    assert name == "projects/proj-x"
    assert len(ts_list) == 1
    ts = ts_list[0]
    assert ts.metric.type == "custom.googleapis.com/agent_traffic/qps"
    assert len(ts.points) == 1
    assert ts.points[0].value.double_value == 12.5


def test_write_gauge_accepts_already_prefixed_type():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    w.write_gauge("custom.googleapis.com/agent_eval/helpfulness", 4.0)
    ts = client.flatten()[0]
    # Must not double-prefix.
    assert ts.metric.type == "custom.googleapis.com/agent_eval/helpfulness"
    assert ts.metric.type.count("custom.googleapis.com/") == 1


def test_write_gauge_resource_matches_quality_alerts():
    """quality_alerts.py filters on resource.type="global" — we must match."""
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    w.write_gauge("agent_eval/helpfulness", 4.2, labels={"engine_id": "e1"})
    ts = client.flatten()[0]
    assert ts.resource.type == "global"
    assert ts.resource.labels["project_id"] == "proj-x"
    assert ts.metric.labels["engine_id"] == "e1"


def test_emit_traffic_metrics_types_and_math():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    summary = {
        "offered": 100,
        "sent": 90,
        "errors": 10,
        "injected": 4,
        "achieved_qps": 8.5,
        "p50_latency": 0.20,
        "p95_latency": 0.55,
        "duration_s": 12.0,
    }
    emit_traffic_metrics(summary, writer=w)

    vals = _by_type(client)
    assert vals["custom.googleapis.com/agent_traffic/qps"] == 8.5
    assert vals["custom.googleapis.com/agent_traffic/request_latency_p50"] == 0.20
    assert vals["custom.googleapis.com/agent_traffic/request_latency_p95"] == 0.55
    assert vals["custom.googleapis.com/agent_traffic/injected"] == 4
    # error_rate = errors / max(offered, 1)
    assert vals["custom.googleapis.com/agent_traffic/error_rate"] == 10 / 100


def test_emit_traffic_metrics_error_rate_zero_offered():
    """Guard against divide-by-zero when nothing was offered."""
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    summary = {
        "offered": 0,
        "sent": 0,
        "errors": 0,
        "injected": 0,
        "achieved_qps": 0.0,
        "p50_latency": 0.0,
        "p95_latency": 0.0,
        "duration_s": 1.0,
    }
    emit_traffic_metrics(summary, writer=w)
    vals = _by_type(client)
    assert vals["custom.googleapis.com/agent_traffic/error_rate"] == 0.0


def test_emit_traffic_extra_labels_applied():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    summary = {
        "offered": 1, "sent": 1, "errors": 0, "injected": 0,
        "achieved_qps": 1.0, "p50_latency": 0.1, "p95_latency": 0.1,
        "duration_s": 1.0,
    }
    emit_traffic_metrics(summary, writer=w, extra_labels={"run": "demo1"})
    for ts in client.flatten():
        assert ts.metric.labels["run"] == "demo1"


def test_quality_metric_types_match_quality_alerts():
    """The quality metric types we expose must match quality_alerts.py exactly."""
    expected = {
        f"custom.googleapis.com/agent_eval/{name}"
        for name, _threshold in ALL_MONITORED_METRICS
    }
    assert set(QUALITY_METRIC_TYPES) == expected
    # Sanity: the known strings are present.
    assert "custom.googleapis.com/agent_eval/helpfulness" in QUALITY_METRIC_TYPES
    assert "custom.googleapis.com/agent_eval/tool_use_accuracy" in QUALITY_METRIC_TYPES


def test_write_quality_scores_emits_all():
    client = FakeMetricClient()
    w = MetricsWriter(project_id="proj-x", client=client)
    metrics.write_quality_scores({"helpfulness": 4.5, "policy_compliance": 3.9}, writer=w)
    vals = _by_type(client)
    assert vals["custom.googleapis.com/agent_eval/helpfulness"] == 4.5
    assert vals["custom.googleapis.com/agent_eval/policy_compliance"] == 3.9
