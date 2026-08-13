"""Offline tests for the router-efficiency monitoring bridge (no live GCP)."""

from src.eval import publish_router_efficiency as router_pub
from src.eval.publish_router_efficiency import publish_router_efficiency


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


def _writer(client):
    from src.observability.metrics import MetricsWriter

    return MetricsWriter(project_id="proj-x", client=client)


def _by_type(client):
    return {ts.metric.type: ts.points[0].value.double_value for ts in client.flatten()}


def test_extract_and_publish_native_units():
    client = FakeMetricClient()
    accuracy = {"accuracy": 0.92, "avg_latency_ms": 145.0}
    cost = {"savings_pct": 60.0}

    published = publish_router_efficiency(accuracy, cost, writer=_writer(client))

    # Returned dict is in native units — accuracy scaled to percent, others verbatim.
    assert published == {
        "routing_accuracy_pct": 92.0,
        "cost_savings_pct": 60.0,
        "classifier_latency_ms": 145.0,
    }
    vals = _by_type(client)
    assert vals["custom.googleapis.com/agent_router/routing_accuracy_pct"] == 92.0
    assert vals["custom.googleapis.com/agent_router/cost_savings_pct"] == 60.0
    assert vals["custom.googleapis.com/agent_router/classifier_latency_ms"] == 145.0


def test_offline_label_applied():
    client = FakeMetricClient()
    publish_router_efficiency(
        {"accuracy": 0.9, "avg_latency_ms": 100.0},
        {"savings_pct": 50.0},
        writer=_writer(client),
    )
    for ts in client.flatten():
        assert ts.metric.labels["eval_mode"] == "offline"


def test_missing_keys_do_not_crash():
    client = FakeMetricClient()
    # Only accuracy present, no latency, no cost block at all.
    published = publish_router_efficiency({"accuracy": 0.8}, None, writer=_writer(client))
    assert published == {"routing_accuracy_pct": 80.0}
    vals = _by_type(client)
    assert vals["custom.googleapis.com/agent_router/routing_accuracy_pct"] == 80.0
    assert "custom.googleapis.com/agent_router/cost_savings_pct" not in vals


def test_empty_inputs_write_nothing():
    client = FakeMetricClient()
    published = publish_router_efficiency({}, {}, writer=_writer(client))
    assert published == {}
    assert client.calls == []


def test_logs_a_router_efficiency_experiment_run_when_named():
    client = FakeMetricClient()
    runs = []

    published = publish_router_efficiency(
        {"accuracy": 0.9, "avg_latency_ms": 100.0},
        {"savings_pct": 55.0},
        writer=_writer(client),
        experiment_name="router-efficiency",
        log_run_fn=lambda **k: runs.append(k) or True,
    )

    # One run into the router's OWN experiment (never the coordinator's).
    assert len(runs) == 1
    assert runs[0]["experiment"] == "router-efficiency"
    assert runs[0]["params"] == {"surface": "router"}
    # The native-unit scores are logged verbatim as the run metrics.
    assert runs[0]["metrics"] == published


def test_experiment_logging_dormant_by_default():
    client = FakeMetricClient()
    runs = []
    publish_router_efficiency(
        {"accuracy": 0.9, "avg_latency_ms": 100.0},
        {"savings_pct": 55.0},
        writer=_writer(client),
        log_run_fn=lambda **k: runs.append(k),
    )
    # No experiment_name -> the helper is invoked but with experiment=None (no-op).
    assert [r["experiment"] for r in runs] == [None]


def test_no_experiment_run_when_nothing_published():
    runs = []
    publish_router_efficiency(
        {}, {}, experiment_name="router-efficiency", log_run_fn=lambda **k: runs.append(k)
    )
    assert runs == []  # nothing to record


def test_label_flag_forwarded_as_extra_labels(tmp_path, monkeypatch):
    import json

    path = tmp_path / "full_results.json"
    path.write_text(
        json.dumps({"complexity": {"accuracy": {"accuracy": 0.9}, "cost_efficiency": {}}})
    )

    captured = {}
    monkeypatch.setattr(
        router_pub,
        "publish_router_efficiency",
        lambda acc, cost, **k: captured.update(k) or {"routing_accuracy_pct": 90.0},
    )
    router_pub.main(["--from-json", str(path), "--label", "model=gemini-3.6-flash"])
    assert captured["extra_labels"] == {"model": "gemini-3.6-flash"}
