"""Offline tests for the bake-off orchestrator (no network, no deploys).

``run_bakeoff`` is a thin chain over the phase entrypoints: DOE fan-out → read
the two engine ids from the manifest → pairwise SxS → per-model labeled traffic →
grouped verify_monitors → bake-off report. Every step is injectable so these
tests assert the *wiring* (dry-run prints a plan and calls nothing; execute flows
the two engine ids through every downstream step) without touching GCP.
"""

import json

from src.doe import run_bakeoff as rb


def test_model_ids_come_from_the_factor():
    # The two backbones are read from the model_backend factor, not hardcoded here.
    baseline, candidate = rb.bakeoff_model_ids()
    assert baseline == "gemini-3.6-flash"
    assert candidate == "claude-sonnet-5"


def test_dry_run_plans_but_calls_nothing():
    calls = []

    result = rb.run_bakeoff(
        dry_run=True,
        experiment_id="exp-1",
        doe_fn=lambda **k: calls.append("doe") or {},
        pairwise_fn=lambda *a, **k: calls.append("pairwise") or {},
        verify_fn=lambda **k: calls.append("verify") or {},
        traffic_runner=lambda *a, **k: calls.append("traffic"),
    )
    assert calls == []  # nothing executed
    assert result["dry_run"] is True
    assert result["baseline_model"] == "gemini-3.6-flash"
    assert result["candidate_model"] == "claude-sonnet-5"
    # The plan lists the ordered phases.
    blob = "\n".join(result["steps"]).lower()
    assert "doe" in blob
    assert "pairwise" in blob
    assert "traffic" in blob
    assert "verify" in blob
    assert "report" in blob


def _manifest():
    return {
        "experiment_id": "exp-1",
        "points": [
            {"assignments": {"model_backend": "gemini"}, "engine_id": "ENG_GEM"},
            {"assignments": {"model_backend": "claude"}, "engine_id": "ENG_CLA"},
        ],
    }


def test_execute_flows_engine_ids_through_every_step(tmp_path):
    import pandas as pd

    df = pd.DataFrame(
        [
            {"model_backend": "gemini", "final_response_quality": 0.7, "tool_use_quality": 0.6},
            {"model_backend": "claude", "final_response_quality": 0.85, "tool_use_quality": 0.8},
        ]
    )
    seen = {}

    def doe_fn(**k):
        seen["doe_kwargs"] = k
        return {"experiment_id": "exp-1", "manifest": _manifest(), "dataframe": df}

    def pairwise_fn(baseline, candidate, **k):
        seen["pairwise"] = (baseline, candidate)
        return {"win_rate_candidate": 0.6, "win_rate_baseline": 0.3, "tie_rate": 0.1}

    traffic_calls = []

    def traffic_runner(engine_id, model, **k):
        traffic_calls.append((engine_id, model))

    def verify_fn(**k):
        seen["verify_kwargs"] = k
        return {
            "coordinator_quality": {
                "group_by": "model",
                "metrics": {
                    "request_latency_p95": {
                        "gemini-3.6-flash": {"avg_score": 2.0},
                        "claude-sonnet-5": {"avg_score": 3.0},
                    }
                },
            }
        }

    result = rb.run_bakeoff(
        dry_run=False,
        experiment_id="exp-1",
        out_dir=str(tmp_path),
        doe_fn=doe_fn,
        pairwise_fn=pairwise_fn,
        traffic_runner=traffic_runner,
        verify_fn=verify_fn,
    )

    # DOE ran as a single-factor full design.
    assert seen["doe_kwargs"]["factor_names"] == ["model_backend"]
    assert seen["doe_kwargs"]["kind"] == "full"
    assert seen["doe_kwargs"]["dry_run"] is False
    # Engine ids from the manifest flow into pairwise (gemini=baseline, claude=candidate).
    assert seen["pairwise"] == ("ENG_GEM", "ENG_CLA")
    # Traffic ran once per engine, each tagged with its model id.
    assert ("ENG_GEM", "gemini-3.6-flash") in traffic_calls
    assert ("ENG_CLA", "claude-sonnet-5") in traffic_calls
    # verify grouped by model.
    assert seen["verify_kwargs"]["group_by"] == "model"
    # The report was written and fuses all four streams.
    report_path = tmp_path / "bakeoff_report.md"
    assert report_path.exists()
    md = report_path.read_text()
    assert "# Coordinator Model Bake-Off" in md
    assert "## Verdict" in md
    assert result["report_path"] == str(report_path)


def test_execute_reads_manifest_from_disk_when_doe_returns_path(tmp_path):
    # If the DOE summary carries no inline manifest, fall back to out_dir/manifest.json.
    import pandas as pd

    (tmp_path / "manifest.json").write_text(json.dumps(_manifest()))
    df = pd.DataFrame([{"model_backend": "gemini", "final_response_quality": 0.7}])

    seen = {}

    def pairwise_fn(b, c, **k):
        seen["pw"] = (b, c)
        return {}

    result = rb.run_bakeoff(
        dry_run=False,
        experiment_id="exp-1",
        out_dir=str(tmp_path),
        doe_fn=lambda **k: {"experiment_id": "exp-1", "dataframe": df},
        pairwise_fn=pairwise_fn,
        traffic_runner=lambda *a, **k: None,
        verify_fn=lambda **k: {},
    )
    assert seen["pw"] == ("ENG_GEM", "ENG_CLA")
    assert result["report_path"].endswith("bakeoff_report.md")
