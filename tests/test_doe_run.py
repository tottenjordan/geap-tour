"""Orchestrator wiring for src.doe.run_doe (no real submits)."""

import types

from src.doe import run_doe


def _fake_runner(recorder):
    def _run(cmd, env=None, capture_output=True, text=True, check=False):
        recorder.append(cmd)
        return types.SimpleNamespace(
            returncode=0,
            stdout="Submitted PipelineJob: projects/p/locations/l/pipelineJobs/1\n",
            stderr="",
        )
    return _run


def test_dry_run_submits_nothing(tmp_path):
    calls: list = []
    summary = run_doe.run_experiment(
        kind="screening",
        experiment_id="expdry",
        dry_run=True,
        spec_dir=str(tmp_path),
        out_dir=str(tmp_path / "out"),
        runner=_fake_runner(calls),
    )
    assert calls == []  # nothing submitted
    assert summary["manifest"]["num_points"] == 9
    assert "dataframe" not in summary


def test_execute_without_wait_submits_all(tmp_path):
    calls: list = []
    summary = run_doe.run_experiment(
        kind="screening",
        experiment_id="expexec",
        dry_run=False,
        wait=False,
        spec_dir=str(tmp_path),
        out_dir=str(tmp_path / "out"),
        runner=_fake_runner(calls),
    )
    assert len(calls) == 9  # one subprocess per design point
    assert summary["manifest"]["num_points"] == 9
    assert "dataframe" not in summary  # no wait => no harvest


def test_max_runs_caps_fanout(tmp_path):
    calls: list = []
    summary = run_doe.run_experiment(
        kind="screening",
        experiment_id="expcap",
        dry_run=False,
        max_runs=2,
        spec_dir=str(tmp_path),
        out_dir=str(tmp_path / "out"),
        runner=_fake_runner(calls),
    )
    assert len(calls) == 2
    assert summary["manifest"]["num_points"] == 2


def test_default_experiment_id_uses_kind_and_timestamp():
    from datetime import datetime

    eid = run_doe._default_experiment_id("screening", now=datetime(2026, 8, 6, 12, 0, 0))
    assert eid == "doe-screening-20260806-120000"


def test_execute_with_wait_harvests_and_analyzes(tmp_path):
    calls: list = []
    # Stub harvest + analyze so we don't touch GCS/aiplatform.
    import src.doe.run_doe as rd
    import pandas as pd

    orig_harvest, orig_analyze = rd.harvest, rd.analyze
    try:
        rd.harvest = lambda manifest, out_dir=".", wait=True: pd.DataFrame([{"design_point": "dp01"}])
        rd.analyze = lambda df, factors, experiment_id, out_dir=".": "# report"
        summary = rd.run_experiment(
            kind="screening",
            experiment_id="expwait",
            dry_run=False,
            wait=True,
            max_runs=1,
            spec_dir=str(tmp_path),
            out_dir=str(tmp_path / "out"),
            runner=_fake_runner(calls),
        )
    finally:
        rd.harvest, rd.analyze = orig_harvest, orig_analyze

    assert summary["report"] == "# report"
    assert "dataframe" in summary
