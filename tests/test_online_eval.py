"""Offline tests for the periodic-snapshot eval publish + verify flow.

Covers the score->metric bridge and the three-surface monitor verification:

* ``publish_eval_metrics`` — bridges eval scores onto the ``agent_eval/*`` series.
* ``verify_monitors``      — reads the canonical source (Cloud Monitoring) as three
  surfaces (coordinator quality + online quality + router efficiency), tolerating
  the absent optional BigQuery ``online_eval_results`` export table.

All Vertex / Cloud Monitoring / BigQuery clients are faked; no credentials.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from src.eval import publish_eval_metrics as bridge
from src.eval import verify_monitors as vm
from src.eval.quality_alerts import ALL_MONITORED_METRICS


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeMetricClient:
    """Records create_time_series calls; no network."""

    def __init__(self):
        self.calls = []

    def create_time_series(self, name=None, time_series=None):
        self.calls.append((name, list(time_series)))

    def flatten(self):
        out = []
        for _name, ts_list in self.calls:
            out.extend(ts_list)
        return out


def _make_series(metric_type: str, values, now=None, labels=None):
    """Build a fake Cloud Monitoring TimeSeries with one point per value."""
    now = now or datetime.now()
    points = [
        SimpleNamespace(
            value=SimpleNamespace(double_value=float(v)),
            interval=SimpleNamespace(end_time=now - timedelta(minutes=i)),
        )
        for i, v in enumerate(values)
    ]
    return SimpleNamespace(
        metric=SimpleNamespace(type=metric_type, labels=labels or {}), points=points
    )


class FakeMonitoringClient:
    """Stand-in that honors the single-metric filter the real API enforces.

    Cloud Monitoring's ``list_time_series`` requires the filter to resolve to
    exactly one ``metric.type`` (a ``starts_with`` prefix that matches multiple
    metrics 400s), so this fake returns only the series whose metric type
    matches an exact ``metric.type = "..."`` filter.
    """

    def __init__(self, series):
        self._series = series
        self.requests = []

    def list_time_series(self, request=None, **kwargs):
        import re

        req = request or kwargs
        self.requests.append(req)
        match = re.search(r'metric\.type = "([^"]+)"', req.get("filter", ""))
        if match:
            return [s for s in self._series if s.metric.type == match.group(1)]
        return list(self._series)


class FakeBQClient:
    """BigQuery client whose ``online_eval_results`` table does not exist."""

    def get_table(self, table_ref):
        raise RuntimeError("404 Not found: Table")


# --------------------------------------------------------------------------- #
# 1. Score -> metric bridge lands on agent_eval/* names from ALL_MONITORED_METRICS
# --------------------------------------------------------------------------- #
def test_bridge_publishes_only_monitored_metric_names():
    client = FakeMetricClient()
    writer = bridge.MetricsWriter(project_id="proj-x", client=client)

    published = bridge.publish_eval_metrics(
        {
            "helpfulness": 4.5,
            "tool_use_accuracy": 4.1,
            "policy_compliance": 3.8,
            "some_unmonitored_metric": 2.0,  # must be dropped
        },
        writer=writer,
    )

    monitored = {name for name, _ in ALL_MONITORED_METRICS}
    assert set(published) == monitored

    emitted = {ts.metric.type: ts.points[0].value.double_value for ts in client.flatten()}
    for name in monitored:
        assert emitted[f"custom.googleapis.com/agent_eval/{name}"] == published[name]
    assert "custom.googleapis.com/agent_eval/some_unmonitored_metric" not in emitted


def test_bridge_maps_evaluator_metric_aliases():
    client = FakeMetricClient()
    writer = bridge.MetricsWriter(project_id="proj-x", client=client)

    published = bridge.publish_eval_metrics(
        {"final_response_quality_v1": 4.2, "tool_use_quality_v1": 3.7},
        writer=writer,
    )
    assert published["helpfulness"] == 4.2
    assert published["tool_use_accuracy"] == 3.7
    emitted = {ts.metric.type for ts in client.flatten()}
    assert "custom.googleapis.com/agent_eval/helpfulness" in emitted


def test_bridge_ignores_none_scores():
    client = FakeMetricClient()
    writer = bridge.MetricsWriter(project_id="proj-x", client=client)
    published = bridge.publish_eval_metrics({"helpfulness": None}, writer=writer)
    assert published == {}
    assert client.calls == []


# --------------------------------------------------------------------------- #
# 2. verify_monitors reads Cloud Monitoring; tolerates the absent BQ table
# --------------------------------------------------------------------------- #
def test_verify_reads_coordinator_quality_surface():
    series = [
        _make_series("custom.googleapis.com/agent_eval/helpfulness", [4.0, 5.0, 3.0]),
        _make_series("custom.googleapis.com/agent_eval/policy_compliance", [2.0, 4.0]),
    ]
    client = FakeMonitoringClient(series)

    data = vm.verify_monitor_results(output_format="json", client=client)

    assert data["status"] == "ok"
    quality = data["coordinator_quality"]
    assert quality["status"] == "ok"
    assert quality["total_evals"] == 5
    assert quality["metrics"]["helpfulness"]["eval_count"] == 3
    # Only 3 points in the window -> below the sample floor -> flagged.
    assert quality["metrics"]["helpfulness"]["low_confidence"] is True
    assert quality["metrics"]["helpfulness"]["avg_score"] == 4.0
    # policy_compliance alerts LT 3.0 -> the 2.0 point is out of bounds.
    assert quality["metrics"]["policy_compliance"]["out_of_bounds"] == 1
    # It must query Cloud Monitoring for the agent_eval/* series.
    assert client.requests
    assert any("agent_eval" in req["filter"] for req in client.requests)


def test_verify_reads_router_efficiency_surface_with_directions():
    series = [
        # accuracy floor is 80.0 (LT): 75.0 is out of bounds, 92.0 is fine.
        _make_series("custom.googleapis.com/agent_router/routing_accuracy_pct", [92.0, 75.0]),
        # latency ceiling is 8000.0 (GT): 9000.0 is out of bounds.
        _make_series("custom.googleapis.com/agent_router/classifier_latency_ms", [150.0, 9000.0]),
    ]
    client = FakeMonitoringClient(series)

    data = vm.verify_monitor_results(output_format="json", client=client)

    router = data["router_efficiency"]
    assert router["status"] == "ok"
    assert router["metrics"]["routing_accuracy_pct"]["out_of_bounds"] == 1
    assert router["metrics"]["routing_accuracy_pct"]["direction"] == "LT"
    assert router["metrics"]["classifier_latency_ms"]["out_of_bounds"] == 1
    assert router["metrics"]["classifier_latency_ms"]["direction"] == "GT"


def test_verify_queries_one_exact_metric_per_request():
    # Regression: list_time_series 400s if the filter matches >1 metric type,
    # so verify must issue an exact-match request per monitored metric rather
    # than a single starts_with prefix that fans out across all agent_eval/*.
    series = [
        _make_series("custom.googleapis.com/agent_eval/helpfulness", [4.0, 5.0]),
        _make_series("custom.googleapis.com/agent_eval/policy_compliance", [3.0]),
    ]
    client = FakeMonitoringClient(series)

    data = vm.verify_monitor_results(output_format="json", client=client)

    # One request per monitored metric across ALL THREE surfaces, each an exact match.
    from src.eval.quality_alerts import ONLINE_MONITORED_METRICS, ROUTER_MONITORED_METRICS

    expected_requests = (
        len(ALL_MONITORED_METRICS) + len(ONLINE_MONITORED_METRICS) + len(ROUTER_MONITORED_METRICS)
    )
    assert len(client.requests) == expected_requests
    for req in client.requests:
        assert "starts_with" not in req["filter"]
        assert req["filter"].startswith("metric.type = ")
    # No double counting despite multiple per-metric requests.
    assert data["coordinator_quality"]["total_evals"] == 3


def test_verify_monitoring_empty_series_no_crash():
    client = FakeMonitoringClient([])
    data = vm.verify_monitor_results(output_format="json", client=client)
    assert data["status"] == "empty"
    assert data["coordinator_quality"]["status"] == "empty"
    assert data["router_efficiency"]["status"] == "empty"


def test_verify_tolerates_missing_metric_descriptor():
    # A metric that was never written has no descriptor; the API 404s for that
    # exact-match query. verify must skip it (no data) rather than aborting the
    # whole multi-surface read.
    from google.api_core import exceptions as gexc

    present = _make_series("custom.googleapis.com/agent_eval/helpfulness", [4.0, 5.0])

    class PartialClient(FakeMonitoringClient):
        def list_time_series(self, request=None, **kwargs):
            req = request or kwargs
            self.requests.append(req)
            # Only helpfulness has ever been written; everything else 404s.
            if "agent_eval/helpfulness" in req.get("filter", ""):
                return [present]
            raise gexc.NotFound("Cannot find metric(s) that match type")

    client = PartialClient([present])
    data = vm.verify_monitor_results(output_format="json", client=client)

    # The present metric still reads back; missing descriptors are just absent.
    assert data["coordinator_quality"]["status"] == "ok"
    assert data["coordinator_quality"]["metrics"]["helpfulness"]["eval_count"] == 2
    assert "policy_compliance" not in data["coordinator_quality"]["metrics"]
    # Surfaces whose every metric 404'd degrade to empty, not a crash.
    assert data["router_efficiency"]["status"] == "empty"


def test_verify_group_by_model_splits_into_per_model_buckets():
    # Same metric type, two deployments distinguished by the ``model`` label.
    series = [
        _make_series(
            "custom.googleapis.com/agent_eval/helpfulness",
            [4.0, 5.0],
            labels={"model": "gemini-3.6-flash"},
        ),
        _make_series(
            "custom.googleapis.com/agent_eval/helpfulness",
            [2.0, 2.5],
            labels={"model": "claude-sonnet-5"},
        ),
    ]
    client = FakeMonitoringClient(series)

    data = vm.verify_monitor_results(output_format="json", client=client, group_by="model")

    quality = data["coordinator_quality"]
    assert quality["status"] == "ok"
    assert quality["group_by"] == "model"
    # Two buckets, one per model, each with its own average — not a merged mean.
    hp = quality["metrics"]["helpfulness"]
    assert set(hp) == {"gemini-3.6-flash", "claude-sonnet-5"}
    assert hp["gemini-3.6-flash"]["avg_score"] == 4.5
    assert hp["claude-sonnet-5"]["avg_score"] == 2.25
    # claude's scores are below the 3.0 floor -> both out of bounds.
    assert hp["claude-sonnet-5"]["out_of_bounds"] == 2
    assert hp["gemini-3.6-flash"]["out_of_bounds"] == 0


def test_grouped_markdown_renders_per_model_rows():
    series = [
        _make_series(
            "custom.googleapis.com/agent_eval/helpfulness",
            [4.0, 5.0],
            labels={"model": "gemini-3.6-flash"},
        ),
        _make_series(
            "custom.googleapis.com/agent_eval/helpfulness",
            [2.0],
            labels={"model": "claude-sonnet-5"},
        ),
    ]
    client = FakeMonitoringClient(series)
    data = vm.verify_monitor_results(output_format="json", client=client, group_by="model")
    md = vm.generate_markdown_report(data)
    assert "| Metric | Model |" in md
    assert "gemini-3.6-flash" in md
    assert "claude-sonnet-5" in md


def test_verify_ungrouped_default_shape_unchanged():
    # Without group_by, metrics[name] is the flat summary (no per-model nesting).
    series = [
        _make_series(
            "custom.googleapis.com/agent_eval/helpfulness",
            [4.0, 2.0],
            labels={"model": "gemini-3.6-flash"},
        ),
    ]
    client = FakeMonitoringClient(series)
    data = vm.verify_monitor_results(output_format="json", client=client)
    hp = data["coordinator_quality"]["metrics"]["helpfulness"]
    assert hp["avg_score"] == 3.0  # merged across both points
    assert "group_by" not in data["coordinator_quality"]


def test_verify_bigquery_missing_table_does_not_crash():
    """The old required BQ table is now optional; its absence must not crash."""
    data = vm.verify_monitor_results(
        output_format="json", source="bigquery", bq_client=FakeBQClient()
    )
    assert data["status"] == "no_table"
