"""Offline tests for the coordinator offline-eval -> agent_eval/* bridge.

``publish_offline_eval.publish_offline_scores`` extracts the coordinator's
monitored quality metrics from a ``run_multi_agent_batch_eval`` result, scales
them 0-1 -> 1-5, and delegates to the shared ``publish_eval_metrics`` bridge.
Router efficiency is a separate surface (see test_publish_router_efficiency).
All Cloud Monitoring clients are faked.
"""

from src.eval import publish_offline_eval as off
from src.eval.publish_eval_metrics import MetricsWriter


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


def _batch(metrics: dict, agent: str = "coordinator_agent") -> dict:
    """Wrap a ``{key: score}`` map in the run_multi_agent_batch_eval shape."""
    return {
        "agents": {
            agent: {"metrics": {k: {"score": v} for k, v in metrics.items()}},
        }
    }


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_scale_0_1_to_5():
    assert off._to_monitored_scale(0.6) == 3.0
    assert off._to_monitored_scale(1.0) == 5.0
    assert off._to_monitored_scale(0.0) == 0.0


def test_strip_engine_prefix():
    assert off._strip_engine_prefix("agent_engine_0/final_response_quality_v1") == (
        "final_response_quality_v1"
    )
    assert off._strip_engine_prefix("policy_compliance") == "policy_compliance"


# --------------------------------------------------------------------------- #
# Extraction + publish
# --------------------------------------------------------------------------- #
def test_extract_and_publish_coordinator_metrics():
    client = FakeMetricClient()
    writer = MetricsWriter(project_id="proj-x", client=client)

    batch = _batch(
        {
            "agent_engine_0/final_response_quality_v1": 0.9,
            "agent_engine_0/tool_use_quality_v1": 0.8,
            "agent_engine_0/policy_compliance": 0.7,
            "agent_engine_0/safety_v1": 1.0,  # not monitored -> dropped
        }
    )
    published = off.publish_offline_scores(batch, writer=writer)

    assert published == {
        "helpfulness": 4.5,
        "tool_use_accuracy": 4.0,
        "policy_compliance": 3.5,
    }
    emitted = {ts.metric.type for ts in client.flatten()}
    assert emitted == {
        "custom.googleapis.com/agent_eval/helpfulness",
        "custom.googleapis.com/agent_eval/tool_use_accuracy",
        "custom.googleapis.com/agent_eval/policy_compliance",
    }
    assert "custom.googleapis.com/agent_eval/safety" not in emitted


def test_eval_mode_label_present():
    client = FakeMetricClient()
    writer = MetricsWriter(project_id="proj-x", client=client)
    off.publish_offline_scores(
        _batch({"agent_engine_0/final_response_quality_v1": 0.9}), writer=writer
    )
    labels = client.flatten()[0].metric.labels
    assert labels["eval_mode"] == "offline"


def test_missing_metrics_no_crash():
    client = FakeMetricClient()
    writer = MetricsWriter(project_id="proj-x", client=client)
    # Only helpfulness present, no tool_use, no complexity.
    published = off.publish_offline_scores(
        _batch({"agent_engine_0/final_response_quality_v1": 0.6}), writer=writer
    )
    assert published == {"helpfulness": 3.0}
    assert len(client.flatten()) == 1


def test_empty_batch_no_crash():
    client = FakeMetricClient()
    writer = MetricsWriter(project_id="proj-x", client=client)
    published = off.publish_offline_scores({}, writer=writer)
    assert published == {}
    assert client.calls == []


# --------------------------------------------------------------------------- #
# Policy compliance targets the run's engine (bake-off correctness)
# --------------------------------------------------------------------------- #
def test_inject_policy_targets_explicit_engine(monkeypatch):
    # A bake-off run scores each deployment separately, so the policy judge must
    # run against the engine passed in — NOT the AGENT_ENGINE_ID env default.
    captured = {}

    def fake_run(arn, **kwargs):
        captured["arn"] = arn
        return {"score": 0.8, "n_scored": 5, "n_total": 5}

    monkeypatch.setattr("src.eval.policy_judge.run_policy_compliance_eval", fake_run)

    batch: dict = {}
    off._inject_policy_compliance(batch, agent_id="ENGINE_X")

    assert "ENGINE_X" in captured["arn"]
    assert captured["arn"].endswith("/reasoningEngines/ENGINE_X")
    metrics = batch["agents"]["coordinator_agent"]["metrics"]
    assert metrics["agent_engine_0/policy_compliance"] == {"score": 0.8}


def test_inject_policy_full_arn_passthrough(monkeypatch):
    # A full resource name is used verbatim (not re-wrapped).
    captured = {}

    def fake_run(arn, **kwargs):
        captured["arn"] = arn
        return {"score": 0.6, "n_scored": 3, "n_total": 3}

    monkeypatch.setattr("src.eval.policy_judge.run_policy_compliance_eval", fake_run)
    full = "projects/p/locations/us-central1/reasoningEngines/999"
    off._inject_policy_compliance({}, agent_id=full)
    assert captured["arn"] == full


def test_inject_policy_defaults_to_env_engine(monkeypatch):
    # With no agent_id, it falls back to the AGENT_ENGINE_ID env default.
    from src.config import AGENT_ENGINE_ID

    captured = {}

    def fake_run(arn, **kwargs):
        captured["arn"] = arn
        return {"score": 0.5, "n_scored": 2, "n_total": 2}

    monkeypatch.setattr("src.eval.policy_judge.run_policy_compliance_eval", fake_run)
    off._inject_policy_compliance({}, agent_id=None)
    assert AGENT_ENGINE_ID in captured["arn"]


# --------------------------------------------------------------------------- #
# CLI load helper
# --------------------------------------------------------------------------- #
def test_load_results_full_results_shape(tmp_path):
    import json

    path = tmp_path / "full_results.json"
    payload = {
        "batch": _batch({"agent_engine_0/final_response_quality_v1": 0.9}),
        "complexity": {"accuracy": {"accuracy": 0.85}},
    }
    path.write_text(json.dumps(payload))

    batch = off._load_results(str(path))
    assert batch == payload["batch"]


def test_load_results_batch_shape(tmp_path):
    import json

    path = tmp_path / "batch_results_x.json"
    payload = _batch({"agent_engine_0/final_response_quality_v1": 0.9})
    path.write_text(json.dumps(payload))

    batch = off._load_results(str(path))
    assert batch == payload


def test_from_json_loads_and_publishes(tmp_path, monkeypatch):
    import json

    path = tmp_path / "full_results.json"
    path.write_text(
        json.dumps(
            {
                "batch": _batch({"agent_engine_0/final_response_quality_v1": 0.9}),
                "complexity": {"accuracy": {"accuracy": 0.85}},
            }
        )
    )

    captured = {}
    monkeypatch.setattr(
        off,
        "publish_offline_scores",
        lambda batch, **k: captured.setdefault("call", batch) or {"helpfulness": 4.5},
    )

    off.main(["--from-json", str(path)])
    batch = captured["call"]
    assert "agents" in batch


def test_label_flag_forwarded_as_extra_labels(tmp_path, monkeypatch):
    import json

    path = tmp_path / "full_results.json"
    path.write_text(json.dumps({"batch": _batch({"agent_engine_0/helpfulness": 0.8})}))

    captured = {}
    monkeypatch.setattr(
        off,
        "publish_offline_scores",
        lambda batch, **k: captured.update(k) or {"helpfulness": 4.0},
    )
    off.main(["--from-json", str(path), "--label", "model=claude-sonnet-5"])
    assert captured["extra_labels"] == {"model": "claude-sonnet-5"}


def test_dry_run_writes_nothing(tmp_path, capsys):
    import json

    path = tmp_path / "full_results.json"
    path.write_text(
        json.dumps(
            {
                "batch": _batch({"agent_engine_0/final_response_quality_v1": 0.9}),
                "complexity": {"accuracy": {"accuracy": 0.85}},
            }
        )
    )

    # --dry-run must not touch Cloud Monitoring: a real MetricsWriter with no
    # injected client would lazily build a live client on first write, so a
    # successful dry run proves nothing was written.
    off.main(["--from-json", str(path), "--dry-run"])
    out = capsys.readouterr().out
    assert "helpfulness" in out
