"""Harvesting/parsing for src.doe.harvest, against a real captured fixture."""

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from src.doe import harvest as h

FIXTURE = Path(__file__).parent / "fixtures" / "full_results.json"


@pytest.fixture
def results():
    return json.loads(FIXTURE.read_text())


def test_parse_batch_metrics_matches_fixture(results):
    m = h.parse_batch_metrics(results, "coordinator_agent")
    # Values captured from the validated 2026-08-05 run (version-suffix agnostic:
    # final_response_match is _v2 in the fixture, others _v1).
    assert m["safety"] == pytest.approx(1.0)
    assert m["hallucination"] == pytest.approx(0.9528301886792451)
    assert m["final_response_quality"] == pytest.approx(0.8552631594632802)
    assert m["instruction_following"] == pytest.approx(0.664335323497653)
    assert m["tool_use_quality"] == pytest.approx(0.42418547209940455)
    assert m["final_response_match"] == pytest.approx(0.41758241758241765)


def test_parse_complexity_matches_fixture(results):
    c = h.parse_complexity(results)
    assert c["routing_accuracy"] == pytest.approx(1.0)
    assert c["savings_pct"] == pytest.approx(63.2)
    assert c["routed_cost_usd"] == pytest.approx(0.17867715)
    assert c["all_opus_cost_usd"] == pytest.approx(0.486)


def test_parse_simulated_matches_fixture(results):
    assert h.parse_simulated(results, "coordinator_agent") == 1.0
    # An agent not present in the simulated block -> NaN.
    assert math.isnan(h.parse_simulated(results, "expense_agent"))


def test_parse_results_has_all_columns(results):
    row = h.parse_results(results)
    for col in (*h.BATCH_METRICS, "routing_accuracy", "savings_pct",
                "routed_cost_usd", "all_opus_cost_usd", "sim_passed"):
        assert col in row


def test_malformed_input_yields_nan_no_crash():
    for bad in ({}, {"batch": None}, {"batch": {"agents": {}}}, "not a dict", None):
        row = h.parse_results(bad)  # must not raise
        assert math.isnan(row["tool_use_quality"])
        assert math.isnan(row["savings_pct"])
        assert math.isnan(row["sim_passed"])


def test_build_dataframe_merges_factors_and_responses(results):
    manifest = {
        "experiment_id": "exp1",
        "factors": ["model_tier", "prompt_variant"],
        "points": [
            {
                "design_point": "dp01",
                "is_baseline": False,
                "assignments": {"model_tier": "upgraded", "prompt_variant": "gepa"},
                "gcs_results": "gs://b/eval-results/doe/exp1/dp01/full_results.json",
            },
            {
                "design_point": "baseline",
                "is_baseline": True,
                "assignments": {"model_tier": "baseline", "prompt_variant": "baseline"},
                "gcs_results": "gs://b/eval-results/doe/exp1/baseline/full_results.json",
            },
        ],
    }
    df = h.build_dataframe(manifest, {"dp01": results, "baseline": {}})
    assert isinstance(df, pd.DataFrame)
    assert list(df["design_point"]) == ["dp01", "baseline"]
    # factor columns present
    assert {"model_tier", "prompt_variant"}.issubset(df.columns)
    # dp01 has real data, baseline (empty results) is NaN
    dp01 = df[df.design_point == "dp01"].iloc[0]
    assert dp01["safety"] == pytest.approx(1.0)
    assert dp01["model_tier"] == "upgraded"
    base = df[df.design_point == "baseline"].iloc[0]
    assert math.isnan(base["safety"])


def test_poll_jobs_terminates_with_injected_state():
    manifest = {
        "points": [
            {"design_point": "dp01", "job_resource": "res/1"},
            {"design_point": "dp02", "job_resource": "res/2"},
            {"design_point": "dp03", "job_resource": None},  # dry/failed submit
        ]
    }
    states = h.poll_jobs(
        manifest,
        interval_s=0,
        get_state=lambda r: "PIPELINE_STATE_SUCCEEDED",
        sleep=lambda s: None,
    )
    assert states == {"dp01": "PIPELINE_STATE_SUCCEEDED", "dp02": "PIPELINE_STATE_SUCCEEDED"}


def test_poll_falls_through_to_gcs_when_state_errors():
    # The observed hang: job actually done + results in GCS, but the PipelineJob
    # API stalls/raises. Poll must infer completion from the results artifact
    # instead of spinning to the timeout.
    manifest = {
        "points": [
            {
                "design_point": "dp01",
                "job_resource": "res/1",
                "gcs_results": "gs://b/dp01/full_results.json",
            }
        ]
    }

    def boom(_resource):
        raise RuntimeError("API stall")

    states = h.poll_jobs(
        manifest,
        interval_s=0,
        get_state=boom,
        results_exist=lambda _uri: True,
        sleep=lambda _s: None,
    )
    assert states == {"dp01": "PIPELINE_STATE_SUCCEEDED"}


def test_poll_infers_done_from_gcs_when_state_nonterminal():
    manifest = {
        "points": [
            {
                "design_point": "dp01",
                "job_resource": "res/1",
                "gcs_results": "gs://b/x",
            }
        ]
    }
    states = h.poll_jobs(
        manifest,
        interval_s=0,
        get_state=lambda _r: "PIPELINE_STATE_RUNNING",
        results_exist=lambda _uri: True,
        sleep=lambda _s: None,
    )
    assert states == {"dp01": "PIPELINE_STATE_SUCCEEDED"}


def test_poll_times_out_bounded_when_neither_state_nor_results():
    # Neither a terminal state nor a results artifact: must give up at the
    # wall-clock timeout, not spin forever.
    manifest = {
        "points": [
            {"design_point": "dp01", "job_resource": "res/1", "gcs_results": "gs://b/x"}
        ]
    }
    calls = {"n": 0}

    def sleep(_s):
        calls["n"] += 1

    states = h.poll_jobs(
        manifest,
        interval_s=1,
        timeout_s=3,
        get_state=lambda _r: "PIPELINE_STATE_RUNNING",
        results_exist=lambda _uri: False,
        sleep=sleep,
    )
    assert states == {"dp01": "PIPELINE_STATE_TIMEOUT"}
    assert calls["n"] <= 4  # bounded by timeout_s/interval_s, not infinite


def test_fetch_results_passes_timeout_to_download():
    seen = {}

    class _Blob:
        def download_as_text(self, timeout=None):
            seen["timeout"] = timeout
            return "{}"

    class _Bucket:
        def blob(self, _p):
            return _Blob()

    class _Client:
        def bucket(self, _b):
            return _Bucket()

    out = h.fetch_results("gs://b/p.json", client=_Client(), timeout=42)
    assert out == {}
    assert seen["timeout"] == 42


def test_harvest_forwards_poll_timeout(tmp_path):
    manifest = {"experiment_id": "exp1", "factors": [], "points": []}
    seen = {}

    def _poll(m, *, timeout_s, interval_s):
        seen["timeout_s"] = timeout_s
        seen["interval_s"] = interval_s
        return {}

    h.harvest(
        manifest,
        out_dir=str(tmp_path),
        wait=True,
        poll_timeout_s=5400,
        poll_interval_s=7,
        fetch=lambda uri: {},
        poll=_poll,
    )
    assert seen == {"timeout_s": 5400, "interval_s": 7}
