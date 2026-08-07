"""Launcher fan-out for src.doe.launch (no real submits — runner is monkeypatched)."""

import types

from src.doe import launch as launch_mod
from src.doe.design import DesignPoint, build_design
from src.doe.factors import get_factors


def _fake_runner(recorder, *, returncode=0, job="projects/p/locations/l/pipelineJobs/123"):
    def _run(cmd, env=None, capture_output=True, text=True, check=False):
        recorder.append({"cmd": cmd, "env": env})
        stdout = f"https://console...\nSubmitted PipelineJob: {job}\n"
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    return _run


def test_build_point_env_merges_env_channels():
    factors = get_factors()
    point = DesignPoint(
        "dp01",
        {
            "router_boundaries": "aggressive_savings",
            "model_tier": "upgraded",
            "prompt_variant": "gepa",
            "eval_fidelity": "quick",  # param channel — excluded from env
        },
    )
    env = launch_mod.build_point_env(point, factors)
    assert env["COMPLEXITY_LOW"] == "0.45"          # runner_env
    assert env["COORDINATOR_MODEL"] == "gemini-3.1-pro-preview"  # engine_env
    assert env["PROMPT_VARIANT"] == "gepa"          # engine_env
    assert "scenario_count" not in env              # param channel excluded


def test_build_point_params_extracts_param_channel():
    factors = get_factors()
    point = DesignPoint("dp01", {
        "router_boundaries": "baseline", "model_tier": "baseline",
        "prompt_variant": "baseline", "eval_fidelity": "thorough",
    })
    params = launch_mod.build_point_params(point, factors)
    assert params == {"scenario_count": 8, "max_turns": 4}


def test_submit_point_builds_correct_cmd_and_env(monkeypatch):
    factors = get_factors()
    point = DesignPoint("dp02", {
        "router_boundaries": "baseline", "model_tier": "upgraded",
        "prompt_variant": "gepa", "eval_fidelity": "quick",
    })
    calls: list[dict] = []
    entry = launch_mod.submit_point(
        point, factors, "exp1", spec_dir="/tmp",
        runner=_fake_runner(calls),
    )
    cmd = calls[0]["cmd"]
    assert "--experiment-id" in cmd and "exp1" in cmd
    assert "--design-point" in cmd and "dp02" in cmd
    assert "--spec-path" in cmd
    # engine_env factor present → fresh deploy via --agent-module
    assert "--agent-module" in cmd and "coordinator_agent" in cmd
    assert "--agent-id" not in cmd
    # param channel → CLI flags
    assert "--scenario-count" in cmd and "3" in cmd
    assert "--max-turns" in cmd and "2" in cmd
    # env carries the engine_env override
    assert calls[0]["env"]["COORDINATOR_MODEL"] == "gemini-3.1-pro-preview"
    # parsed job resource
    assert entry["job_resource"] == "projects/p/locations/l/pipelineJobs/123"
    assert entry["fresh_deploy"] is True
    assert entry["gcs_prefix"] == "eval-results/doe/exp1/dp02"


def test_submit_point_reuse_when_no_engine_env(monkeypatch):
    # runner_env + param only → can reuse an engine (no fresh deploy)
    factors = get_factors(["router_boundaries", "eval_fidelity"])
    point = DesignPoint("dp01", {"router_boundaries": "baseline", "eval_fidelity": "quick"})
    calls: list[dict] = []
    entry = launch_mod.submit_point(
        point, factors, "exp2", reuse_agent_id="ENGINE123",
        runner=_fake_runner(calls),
    )
    cmd = calls[0]["cmd"]
    assert "--agent-id" in cmd and "ENGINE123" in cmd
    assert "--agent-module" not in cmd
    assert entry["fresh_deploy"] is False


def test_submit_point_failure_captured(monkeypatch):
    factors = get_factors()
    point = build_design(factors, "screening")[0]
    calls: list[dict] = []
    entry = launch_mod.submit_point(
        point, factors, "exp3",
        runner=_fake_runner(calls, returncode=1),
    )
    assert entry["returncode"] == 1
    assert entry["job_resource"] is None


def test_submit_point_dry_run_does_not_call_runner():
    factors = get_factors()
    point = build_design(factors, "screening")[0]
    calls: list[dict] = []
    entry = launch_mod.submit_point(
        point, factors, "expdry", dry_run=True, runner=_fake_runner(calls),
    )
    assert calls == []  # nothing submitted
    assert entry["job_resource"] is None
    assert "cmd" in entry


def test_launch_writes_manifest(tmp_path, monkeypatch):
    factors = get_factors()
    design = build_design(factors, "screening")
    calls: list[dict] = []
    manifest = launch_mod.launch(
        design, factors, "exp42",
        spec_dir=str(tmp_path), out_dir=str(tmp_path / "out"),
        runner=_fake_runner(calls),
    )
    assert manifest["experiment_id"] == "exp42"
    assert manifest["num_points"] == 9
    assert manifest["fresh_deploys"] == 9  # engine_env factors in the set
    assert len(manifest["points"]) == 9
    assert (tmp_path / "out" / "manifest.json").exists()
    # one subprocess per design point
    assert len(calls) == 9
