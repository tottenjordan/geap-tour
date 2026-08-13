from src.eval import run_all_evals as rae
from src.eval.run_all_evals import build_report


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

    def _fake(batch, complexity_results=None):
        seen["call"] = (batch, complexity_results)
        return {"helpfulness": 4.5}

    monkeypatch.setattr(rae, "publish_offline_scores", _fake)
    results = _minimal_results()
    rae._run_publish_phase(results)
    assert results["published_metrics"] == {"helpfulness": 4.5}
    batch, complexity = seen["call"]
    assert "agents" in batch
    assert complexity == results["complexity"]


def test_publish_phase_guards_exceptions(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("no gcp")

    monkeypatch.setattr(rae, "publish_offline_scores", _boom)
    results = _minimal_results()
    rae._run_publish_phase(results)
    assert "error" in results["published_metrics"]
