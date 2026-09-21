import pytest

from src.eval import run_all_evals as rae
from src.eval.run_all_evals import build_report


@pytest.fixture(autouse=True)
def _stub_router_publish(monkeypatch):
    """`_run_publish_phase` publishes TWO surfaces; these tests only ever cared
    about the coordinator one.

    Four tests patched `publish_offline_scores` and left `publish_router_efficiency`
    alone, so the router half ran for real — against whatever ADC the developer
    had. Locally that succeeded and wrote 60.0 (the fixture value from
    `_minimal_results`) into `agent_router/cost_savings_pct`, a series with a live
    alert on it; the junk points dragged its rolling baseline to z=-3.33 and
    `verify_monitors` reported an anomaly nobody had caused. In CI there are no
    credentials, the write fails, and the failure is swallowed because publishing
    is guarded telemetry — so the suite was green either way.

    Stubbed by default here rather than in each test: the next test to call
    `_run_publish_phase` would otherwise inherit the same trap. Tests that
    genuinely assert the router publish (`test_publish_phase_populates_router_metrics`)
    monkeypatch over this, and the later patch wins.
    """
    monkeypatch.setattr(rae, "publish_router_efficiency", lambda *a, **k: {})


def _minimal_results():
    return {
        "run_id": "test_run",
        "timestamp": "2026-08-05T00:00:00",
        "agent": "coordinator_agent",
        "threshold": 3.0,
        "batch": {
            "agents": {
                "coordinator_agent": {
                    "status": "PASSED",
                    "test_cases": 5,
                    "metrics": {"response_quality": {"score": 4.5}},
                }
            }
        },
        "simulated": {"coordinator_agent": {"passed": True}},
        "complexity": {
            "accuracy": {"accuracy_pct": "80%"},
            "cost_efficiency": {
                "savings_pct": 60,
                "routed_cost_usd": 0.001,
                "all_opus_cost_usd": 0.01,
            },
        },
        "monitors": {},
    }


def test_build_report_returns_markdown_string():
    md = build_report(_minimal_results())
    assert isinstance(md, str)
    assert "# GEAP Comprehensive Evaluation Report" in md
    assert "## Batch Evaluation Results" in md
    assert "## Simulated Evaluation Results" in md
    assert "## Complexity Routing Evaluation" in md
    assert "coordinator_agent" in md


def test_build_report_does_no_file_io(tmp_path):
    # build_report must be pure: returns a string, writes nothing.
    md = build_report(_minimal_results())
    assert md.strip().startswith("# GEAP Comprehensive Evaluation Report")
    # no files created in cwd/tmp by the call
    assert list(tmp_path.iterdir()) == []


def test_publish_phase_populates_published_metrics(monkeypatch):
    seen = {}

    def _fake(batch, **kwargs):
        seen["call"] = batch
        return {"helpfulness": 4.5}

    monkeypatch.setattr(rae, "_apply_standalone_judges", lambda *a, **k: None)
    monkeypatch.setattr(rae, "publish_offline_scores", _fake)
    results = _minimal_results()
    rae._run_publish_phase(results)
    assert results["published_metrics"] == {"helpfulness": 4.5}
    assert "agents" in seen["call"]


def test_publish_phase_guards_exceptions(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no gcp")

    monkeypatch.setattr(rae, "_apply_standalone_judges", lambda *a, **k: None)
    monkeypatch.setattr(rae, "publish_offline_scores", _boom)
    results = _minimal_results()
    rae._run_publish_phase(results)
    assert "error" in results["published_metrics"]


def test_publish_phase_applies_standalone_judges_before_publish(monkeypatch):
    # The standalone judges (delegation-aware tool_use + policy) must be spliced
    # into the batch BEFORE publish_offline_scores reads it, and a judge failure
    # must not abort the phase.
    order = []
    monkeypatch.setattr(rae, "_apply_standalone_judges", lambda *a, **k: order.append("judges"))
    monkeypatch.setattr(
        rae, "publish_offline_scores", lambda *a, **k: order.append("publish") or {}
    )
    rae._run_publish_phase(_minimal_results())
    assert order == ["judges", "publish"]


def test_publish_phase_guards_standalone_judge_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no gcp")

    monkeypatch.setattr(rae, "_apply_standalone_judges", _boom)
    monkeypatch.setattr(rae, "publish_offline_scores", lambda *a, **k: {"helpfulness": 4.0})
    results = _minimal_results()
    rae._run_publish_phase(results)  # must not raise
    assert results["published_metrics"] == {"helpfulness": 4.0}


def test_publish_phase_populates_router_metrics(monkeypatch):
    seen = {}

    def _fake_router(accuracy_results, cost_results, **kwargs):
        seen["accuracy"] = accuracy_results
        seen["cost"] = cost_results
        return {
            "classifier_accuracy_pct": 92.0,
            "cost_savings_pct": 60.0,
            "classifier_latency_ms": 150.0,
        }

    monkeypatch.setattr(rae, "_apply_standalone_judges", lambda *a, **k: None)
    monkeypatch.setattr(rae, "publish_offline_scores", lambda *a, **k: {})
    monkeypatch.setattr(rae, "publish_router_efficiency", _fake_router)
    results = _minimal_results()
    rae._run_publish_phase(results)
    assert results["published_router_metrics"]["cost_savings_pct"] == 60.0
    # It forwards the complexity accuracy + cost_efficiency sub-dicts.
    assert seen["accuracy"] == results["complexity"]["accuracy"]
    assert seen["cost"] == results["complexity"]["cost_efficiency"]


def test_publish_phase_router_guards_exceptions(monkeypatch):
    monkeypatch.setattr(rae, "_apply_standalone_judges", lambda *a, **k: None)
    monkeypatch.setattr(rae, "publish_offline_scores", lambda *a, **k: {})

    def _boom(*a, **k):
        raise RuntimeError("no gcp")

    monkeypatch.setattr(rae, "publish_router_efficiency", _boom)
    results = _minimal_results()
    rae._run_publish_phase(results)
    assert "error" in results["published_router_metrics"]


def test_build_report_has_router_efficiency_section():
    results = _minimal_results()
    results["published_router_metrics"] = {
        "classifier_accuracy_pct": 92.0,
        "cost_savings_pct": 60.0,
        "classifier_latency_ms": 150.0,
    }
    md = build_report(results)
    assert "## Router Efficiency" in md
    assert "92.0" in md
    assert "60.0" in md
    assert "150.0" in md
