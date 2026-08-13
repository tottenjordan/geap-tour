"""Offline tests for the bake-off orchestrator (no network, no deploys).

The bake-off deploys two *persistent* coordinator engines (one per backbone,
each in its own interpreter so ``COORDINATOR_MODEL`` bakes at import), scores each
deployed engine offline, measures its real token usage for cost, runs pairwise +
labeled traffic + grouped verify, writes a report, and tears the engines down in a
guaranteed ``finally`` (unless ``--keep-engines``). Every phase is injectable so
these tests assert the *wiring* without touching GCP.
"""

import json
import re

import pytest

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
        deploy_fn=lambda *a, **k: calls.append("deploy") or "ENG",
        score_fn=lambda *a, **k: calls.append("score") or {},
        usage_fn=lambda *a, **k: calls.append("usage") or [],
        pairwise_fn=lambda *a, **k: calls.append("pairwise") or {},
        verify_fn=lambda **k: calls.append("verify") or {},
        traffic_runner=lambda *a, **k: calls.append("traffic"),
        teardown_fn=lambda *a, **k: calls.append("teardown"),
    )
    assert calls == []  # nothing executed
    assert result["dry_run"] is True
    assert result["baseline_model"] == "gemini-3.6-flash"
    assert result["candidate_model"] == "claude-sonnet-5"
    # The plan lists the ordered phases.
    blob = "\n".join(result["steps"]).lower()
    for word in ("deploy", "score", "pairwise", "traffic", "verify", "report"):
        assert word in blob


def _stub_verify():
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


def test_execute_deploys_scores_and_flows_through_every_step(tmp_path):
    seen = {}
    deploys = []
    scores = []
    usages = []
    traffic_calls = []
    teardowns = []

    def deploy_fn(model_id, **k):
        deploys.append(model_id)
        return f"ENG_{model_id}"

    def score_fn(engine_id, model_id, **k):
        scores.append((engine_id, model_id))
        # Higher rubric scores for the candidate so the verdict has a clear winner.
        base = 0.7 if model_id == "gemini-3.6-flash" else 0.85
        return {"final_response_quality": base, "tool_use_quality": base - 0.1}

    def usage_fn(engine_id, model_id, **k):
        usages.append((engine_id, model_id))
        return [{"input_tokens": 1000, "output_tokens": 500}]

    def pairwise_fn(baseline, candidate, **k):
        seen["pairwise"] = (baseline, candidate)
        return {"win_rate_candidate": 0.6, "win_rate_baseline": 0.3, "tie_rate": 0.1}

    def traffic_runner(engine_id, model, **k):
        traffic_calls.append((engine_id, model))

    def verify_fn(**k):
        seen["verify_kwargs"] = k
        return _stub_verify()

    result = rb.run_bakeoff(
        dry_run=False,
        experiment_id="exp-1",
        out_dir=str(tmp_path),
        preflight_fn=lambda ids: seen.setdefault("preflight", list(ids)),
        deploy_fn=deploy_fn,
        score_fn=score_fn,
        usage_fn=usage_fn,
        pairwise_fn=pairwise_fn,
        traffic_runner=traffic_runner,
        verify_fn=verify_fn,
        teardown_fn=lambda eng: teardowns.append(eng),
    )

    # Preflight checked both backbones before anything deployed.
    assert seen["preflight"] == ["gemini-3.6-flash", "claude-sonnet-5"]
    # Deployed once per backbone.
    assert deploys == ["gemini-3.6-flash", "claude-sonnet-5"]
    # Scored + usage-measured each deployed engine.
    assert scores == [
        ("ENG_gemini-3.6-flash", "gemini-3.6-flash"),
        ("ENG_claude-sonnet-5", "claude-sonnet-5"),
    ]
    assert usages == scores
    # Engine ids flow into pairwise (gemini=baseline, claude=candidate).
    assert seen["pairwise"] == ("ENG_gemini-3.6-flash", "ENG_claude-sonnet-5")
    # Traffic ran once per engine, each tagged with its model id.
    assert ("ENG_gemini-3.6-flash", "gemini-3.6-flash") in traffic_calls
    assert ("ENG_claude-sonnet-5", "claude-sonnet-5") in traffic_calls
    # verify grouped by model.
    assert seen["verify_kwargs"]["group_by"] == "model"
    # Engines torn down at the end (both).
    assert set(teardowns) == {"ENG_gemini-3.6-flash", "ENG_claude-sonnet-5"}

    # A manifest with both engine ids was written (usable by pairwise --from-manifest).
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    from src.eval.pairwise_eval import load_engines_from_manifest

    assert load_engines_from_manifest(manifest) == (
        "ENG_gemini-3.6-flash",
        "ENG_claude-sonnet-5",
    )

    # The report fuses all streams AND shows a real per-request cost (not n/a).
    report_path = tmp_path / "bakeoff_report.md"
    md = report_path.read_text()
    assert "# Coordinator Model Bake-Off" in md
    assert "## Verdict" in md
    assert "Cost (fair per-request)" in md
    assert "n/a" not in md.split("Cost (fair per-request)")[1].split("##")[0]
    assert result["report_path"] == str(report_path)
    # Cost surfaced per model in the returned result too.
    assert set(result["cost"]) == {"gemini-3.6-flash", "claude-sonnet-5"}
    assert result["cost"]["claude-sonnet-5"] > 0


def test_keep_engines_skips_teardown(tmp_path):
    teardowns = []
    rb.run_bakeoff(
        dry_run=False,
        experiment_id="exp-1",
        out_dir=str(tmp_path),
        keep_engines=True,
        skip_preflight=True,
        deploy_fn=lambda m, **k: f"ENG_{m}",
        score_fn=lambda e, m, **k: {"final_response_quality": 0.8},
        usage_fn=lambda e, m, **k: [{"input_tokens": 10, "output_tokens": 5}],
        pairwise_fn=lambda b, c, **k: {},
        traffic_runner=lambda *a, **k: None,
        verify_fn=lambda **k: {},
        teardown_fn=lambda eng: teardowns.append(eng),
    )
    assert teardowns == []  # kept, not deleted


def test_teardown_runs_even_when_a_phase_raises(tmp_path):
    teardowns = []

    def boom(*a, **k):
        raise RuntimeError("pairwise blew up")

    with pytest.raises(RuntimeError, match="pairwise blew up"):
        rb.run_bakeoff(
            dry_run=False,
            experiment_id="exp-1",
            out_dir=str(tmp_path),
            skip_preflight=True,
            deploy_fn=lambda m, **k: f"ENG_{m}",
            score_fn=lambda e, m, **k: {"final_response_quality": 0.8},
            usage_fn=lambda e, m, **k: [{"input_tokens": 10, "output_tokens": 5}],
            pairwise_fn=boom,
            traffic_runner=lambda *a, **k: None,
            verify_fn=lambda **k: {},
            teardown_fn=lambda eng: teardowns.append(eng),
        )
    # Both engines still torn down despite the mid-run failure.
    assert set(teardowns) == {"ENG_gemini-3.6-flash", "ENG_claude-sonnet-5"}


def test_cost_is_na_when_no_usage_measured(tmp_path):
    result = rb.run_bakeoff(
        dry_run=False,
        experiment_id="exp-1",
        out_dir=str(tmp_path),
        skip_preflight=True,
        deploy_fn=lambda m, **k: f"ENG_{m}",
        score_fn=lambda e, m, **k: {"final_response_quality": 0.8},
        # No usage surfaced (e.g. traces stripped) -> honest n/a, not a fake $0.
        usage_fn=lambda e, m, **k: [{"input_tokens": 0, "output_tokens": 0}],
        pairwise_fn=lambda b, c, **k: {},
        traffic_runner=lambda *a, **k: None,
        verify_fn=lambda **k: {},
        teardown_fn=lambda eng: None,
    )
    assert result["cost"] == {}
    md = (tmp_path / "bakeoff_report.md").read_text()
    cost_block = md.split("Cost (fair per-request)")[1].split("##")[0]
    assert "n/a" in cost_block


def test_preflight_failure_aborts_before_any_deploy(tmp_path):
    from src.eval.preflight import ModelNotServedError

    called = []

    def failing_preflight(ids):
        raise ModelNotServedError("claude-sonnet-5 not served")

    with pytest.raises(ModelNotServedError):
        rb.run_bakeoff(
            dry_run=False,
            experiment_id="exp-1",
            out_dir=str(tmp_path),
            preflight_fn=failing_preflight,
            deploy_fn=lambda *a, **k: called.append("deploy") or "ENG",
            score_fn=lambda *a, **k: called.append("score") or {},
            usage_fn=lambda *a, **k: called.append("usage") or [],
            pairwise_fn=lambda *a, **k: called.append("pairwise") or {},
            traffic_runner=lambda *a, **k: called.append("traffic"),
            verify_fn=lambda **k: called.append("verify") or {},
            teardown_fn=lambda *a, **k: called.append("teardown"),
        )
    # Nothing downstream ran — no deploy, no spend.
    assert called == []


def test_skip_preflight_bypasses_the_check(tmp_path):
    called = []

    rb.run_bakeoff(
        dry_run=False,
        experiment_id="exp-1",
        out_dir=str(tmp_path),
        skip_preflight=True,
        preflight_fn=lambda ids: called.append("preflight"),
        deploy_fn=lambda m, **k: f"ENG_{m}",
        score_fn=lambda e, m, **k: {"final_response_quality": 0.7},
        usage_fn=lambda e, m, **k: [{"input_tokens": 10, "output_tokens": 5}],
        pairwise_fn=lambda b, c, **k: {},
        traffic_runner=lambda *a, **k: None,
        verify_fn=lambda **k: {},
        teardown_fn=lambda eng: None,
    )
    assert called == []  # preflight skipped entirely


def test_dry_run_never_preflights():
    called = []
    rb.run_bakeoff(
        dry_run=True,
        preflight_fn=lambda ids: called.append("preflight"),
    )
    assert called == []


# --------------------------------------------------------------------------- #
# Unit tests for the real default helpers (still no network).
# --------------------------------------------------------------------------- #
def test_quality_from_batch_maps_versioned_keys_to_base_names():
    agent_result = {
        "metrics": {
            "agent_engine_0/final_response_quality_v1": {"score": 0.82},
            "agent_engine_0/tool_use_quality_v1": {"score": 0.6},
            "agent_engine_0/not_a_rubric_v1": {"score": 0.99},
        }
    }
    q = rb._quality_from_batch(agent_result)
    assert q == {"final_response_quality": 0.82, "tool_use_quality": 0.6}


def test_cost_from_usages_returns_mean_per_request():
    usages = [
        {"input_tokens": 1000, "output_tokens": 500},
        {"input_tokens": 3000, "output_tokens": 100},
    ]
    from src.eval.cost_model import cost_summary

    expected = cost_summary("gemini-3.6-flash", usages)["mean_usd_per_request"]
    assert rb._cost_from_usages("gemini-3.6-flash", usages) == pytest.approx(expected)


def test_cost_from_usages_none_when_zero_tokens():
    assert rb._cost_from_usages("gemini-3.6-flash", []) is None
    assert (
        rb._cost_from_usages("gemini-3.6-flash", [{"input_tokens": 0, "output_tokens": 0}]) is None
    )


def test_collect_token_usage_reads_usage_metadata_from_stream():
    class FakeEngine:
        def stream_query(self, *, user_id, message):
            # Two events: a tool-call event then the final answer, each carrying
            # usage_metadata (prompt count is cumulative; output accrues).
            yield {"usage_metadata": {"prompt_token_count": 120, "candidates_token_count": 30}}
            yield {"usage_metadata": {"prompt_token_count": 120, "candidates_token_count": 45}}

    usages = rb.collect_token_usage(FakeEngine(), ["hi", "there"])
    assert usages == [
        {"input_tokens": 120, "output_tokens": 75},
        {"input_tokens": 120, "output_tokens": 75},
    ]


def test_deploy_engine_captures_resource_from_subprocess_stdout():
    from src.doe.deploy_coordinator import RESOURCE_MARKER

    captured = {}

    class Result:
        returncode = 0
        stdout = f"chatty log\n{RESOURCE_MARKER}projects/p/locations/global/reasoningEngines/77\n"
        stderr = ""

    def fake_runner(cmd, env=None, **k):
        captured["cmd"] = cmd
        captured["model_env"] = env.get("COORDINATOR_MODEL")
        return Result()

    engine = rb._deploy_engine("claude-sonnet-5", runner=fake_runner)
    assert engine == "projects/p/locations/global/reasoningEngines/77"
    # The subprocess baked this point's backbone into COORDINATOR_MODEL.
    assert captured["model_env"] == "claude-sonnet-5"
    assert "src.doe.deploy_coordinator" in captured["cmd"]


def test_deploy_engine_raises_on_nonzero_returncode():
    class Result:
        returncode = 1
        stdout = ""
        stderr = "boom"

    with pytest.raises(RuntimeError, match=re.escape("deploy of gemini-3.6-flash failed")):
        rb._deploy_engine("gemini-3.6-flash", runner=lambda *a, **k: Result())
