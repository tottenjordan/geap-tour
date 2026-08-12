"""Offline tests for the consolidated continuous online-evaluation flow.

Covers the three reconciled modules plus the score->metric bridge:

* ``setup_online_evaluators`` — canonical native onlineEvaluator setup.
* ``publish_eval_metrics``   — bridges eval scores onto ``agent_eval/*`` series.
* ``verify_monitors``        — reads the canonical source (Cloud Monitoring),
  no longer hard-requiring the missing BigQuery ``online_eval_results`` table.
* ``setup_online_monitors``  — thin deprecation shim that delegates to canonical.

All Vertex / Cloud Monitoring / BigQuery clients are faked; no credentials.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from src.eval import publish_eval_metrics as bridge
from src.eval import setup_online_evaluators as soe
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


class FakeResp:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class FakeRequests:
    """Minimal stand-in for the ``requests`` module used by setup_online_evaluators."""

    def __init__(self):
        self.posts = []

    def get(self, url, headers=None):
        return FakeResp({"onlineEvaluators": [], "evaluationMetrics": []})

    def post(self, url, headers=None, json=None):
        self.posts.append((url, json))
        return FakeResp({"name": "operations/xyz"})


def _make_series(metric_type: str, values, now=None):
    """Build a fake Cloud Monitoring TimeSeries with one point per value."""
    now = now or datetime.now()
    points = [
        SimpleNamespace(
            value=SimpleNamespace(double_value=float(v)),
            interval=SimpleNamespace(end_time=now - timedelta(minutes=i)),
        )
        for i, v in enumerate(values)
    ]
    return SimpleNamespace(metric=SimpleNamespace(type=metric_type), points=points)


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
# 1. Canonical setup targets the coordinator with the right sample rate + metrics
# --------------------------------------------------------------------------- #
def test_build_evaluator_config_targets_engine_with_metrics():
    cfg = soe._build_evaluator_config(
        "coordinator", "ENGINE123", ["projects/x/evaluationMetrics/m1"], sample_rate=25
    )
    assert cfg["agentResource"].endswith("reasoningEngines/ENGINE123")
    assert cfg["config"]["randomSampling"]["percentage"] == 25
    # Predefined metric set present, plus the custom metric resource.
    predefined = {
        ms["metric"]["predefinedMetricSpec"]["metricSpecName"]
        for ms in cfg["metricSources"]
        if "metric" in ms
    }
    assert set(soe.PREDEFINED_METRICS) == predefined
    custom = {ms["metricResourceName"] for ms in cfg["metricSources"] if "metricResourceName" in ms}
    assert "projects/x/evaluationMetrics/m1" in custom


def test_create_evaluators_posts_coordinator_monitor(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(soe, "requests", fake)
    monkeypatch.setattr(soe, "_get_headers", dict)
    monkeypatch.setattr(soe, "register_custom_metrics", list)

    soe.create_evaluators(sample_rate=42)

    posted = [json for url, json in fake.posts if url.endswith("/onlineEvaluators")]
    assert posted, "expected at least one onlineEvaluator POST"
    coordinator = [
        p for p in posted if p["agentResource"].endswith(f"reasoningEngines/{soe.COORDINATOR_ENGINE_ID}")
    ]
    assert len(coordinator) == 1
    assert coordinator[0]["config"]["randomSampling"]["percentage"] == 42


# --------------------------------------------------------------------------- #
# 2. Score -> metric bridge lands on agent_eval/* names from ALL_MONITORED_METRICS
# --------------------------------------------------------------------------- #
def test_bridge_publishes_only_monitored_metric_names():
    client = FakeMetricClient()
    writer = bridge.MetricsWriter(project_id="proj-x", client=client)

    published = bridge.publish_eval_metrics(
        {
            "helpfulness": 4.5,
            "tool_use_accuracy": 4.1,
            "policy_compliance": 3.8,
            "complexity_routing_accuracy": 4.9,
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
# 3. verify_monitors reads Cloud Monitoring; tolerates the absent BQ table
# --------------------------------------------------------------------------- #
def test_verify_reads_from_monitoring_series():
    series = [
        _make_series("custom.googleapis.com/agent_eval/helpfulness", [4.0, 5.0, 3.0]),
        _make_series("custom.googleapis.com/agent_eval/policy_compliance", [2.0, 4.0]),
    ]
    client = FakeMonitoringClient(series)

    data = vm.verify_monitor_results(output_format="json", client=client)

    assert data["status"] == "ok"
    assert data["total_evals"] == 5
    assert data["metrics"]["helpfulness"]["eval_count"] == 3
    assert data["metrics"]["helpfulness"]["avg_score"] == 4.0
    assert data["metrics"]["policy_compliance"]["below_threshold"] == 1
    # It must query Cloud Monitoring for the agent_eval/* series.
    assert client.requests
    assert "agent_eval" in client.requests[0]["filter"]


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

    # One request per monitored metric, each an exact metric.type match.
    assert len(client.requests) == len(ALL_MONITORED_METRICS)
    for req in client.requests:
        assert "starts_with" not in req["filter"]
        assert req["filter"].startswith("metric.type = ")
    # No double counting despite multiple per-metric requests.
    assert data["total_evals"] == 3


def test_verify_monitoring_empty_series_no_crash():
    client = FakeMonitoringClient([])
    data = vm.verify_monitor_results(output_format="json", client=client)
    assert data["status"] == "empty"


def test_verify_bigquery_missing_table_does_not_crash():
    """The old required BQ table is now optional; its absence must not crash."""
    data = vm.verify_monitor_results(
        output_format="json", source="bigquery", bq_client=FakeBQClient()
    )
    assert data["status"] == "no_table"


# --------------------------------------------------------------------------- #
# 4. Deprecation shim delegates to the canonical setup
# --------------------------------------------------------------------------- #
def test_shim_delegates_to_canonical(monkeypatch):
    from src.eval import setup_online_monitors as shim

    called = {}
    monkeypatch.setattr(soe, "create_evaluators", lambda *a, **k: called.setdefault("hit", True))
    shim.main([])
    assert called.get("hit") is True
