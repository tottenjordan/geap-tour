"""Offline tests for the multi-stage scaling profile (no live GCP).

`generate_scaling_profile` runs a staircase of QPS stages back-to-back on top of
`generate_load`, emitting per-stage `agent_traffic/*` metrics tagged with a
`stage`/`target_qps` label so the dashboard renders a scaling curve. Tests use a
fake agent + virtual clock (deterministic, fast) and a fake metric client.
"""

import pytest

from src.observability.metrics import MetricsWriter
from src.traffic.generate_traffic import SCALING_STAGES, generate_scaling_profile

from tests.test_traffic_load import FakeAgent, FakeClock
from tests.test_metrics import FakeMetricClient


def test_scaling_runs_every_stage_in_order():
    clock = FakeClock()
    agent = FakeAgent()
    stages = [
        {"qps": 4, "duration_s": 5.0},
        {"qps": 8, "duration_s": 5.0},
        {"qps": 12, "duration_s": 5.0},
    ]
    result = generate_scaling_profile(
        agent,
        stages=stages,
        workers=8,
        seed=0,
        tick_s=0.1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert len(result["stages"]) == 3
    for i, (stage, spec) in enumerate(zip(result["stages"], stages)):
        assert stage["stage"] == i
        assert stage["target_qps"] == spec["qps"]
        # ramp defaults to 0 -> offered ~= qps * duration.
        assert stage["offered"] == pytest.approx(spec["qps"] * spec["duration_s"], rel=0.2)
        assert stage["sent"] == stage["offered"]  # fake agent never fails


def test_scaling_totals_and_peak():
    clock = FakeClock()
    agent = FakeAgent()
    stages = [{"qps": 2, "duration_s": 4.0}, {"qps": 10, "duration_s": 4.0}]
    result = generate_scaling_profile(
        agent, stages=stages, seed=1, tick_s=0.1,
        sleep=clock.sleep, monotonic=clock.monotonic,
    )
    assert result["total_offered"] == sum(s["offered"] for s in result["stages"])
    assert result["total_sent"] == sum(s["sent"] for s in result["stages"])
    assert result["total_errors"] == sum(s["errors"] for s in result["stages"])
    # Peak achieved QPS is the max across stages (the last, higher-QPS stage).
    assert result["peak_qps"] == max(s["achieved_qps"] for s in result["stages"])


def test_scaling_emits_per_stage_labeled_metrics():
    clock = FakeClock()
    agent = FakeAgent()
    client = FakeMetricClient()
    writer = MetricsWriter(project_id="proj-x", client=client)
    stages = [{"qps": 3, "duration_s": 3.0}, {"qps": 6, "duration_s": 3.0}]
    generate_scaling_profile(
        agent, stages=stages, seed=2, tick_s=0.1,
        sleep=clock.sleep, monotonic=clock.monotonic,
        emit_metrics=True, metrics_writer=writer,
    )
    # 5 traffic gauges per stage.
    series = client.flatten()
    assert len(series) == 5 * len(stages)
    # Every series carries the stage-scoping labels.
    stage_labels = {(ts.metric.labels["stage"], ts.metric.labels["target_qps"]) for ts in series}
    assert stage_labels == {("0", "3"), ("1", "6")}


def test_scaling_no_metrics_by_default():
    clock = FakeClock()
    agent = FakeAgent()
    client = FakeMetricClient()
    writer = MetricsWriter(project_id="proj-x", client=client)
    generate_scaling_profile(
        agent, stages=[{"qps": 2, "duration_s": 2.0}], seed=3, tick_s=0.1,
        sleep=clock.sleep, monotonic=clock.monotonic,
        metrics_writer=writer,  # provided, but emit_metrics defaults False
    )
    assert client.calls == []


def test_scaling_on_stage_hook_called_per_stage():
    clock = FakeClock()
    agent = FakeAgent()
    seen = []
    stages = [{"qps": 2, "duration_s": 2.0}, {"qps": 4, "duration_s": 2.0}]
    generate_scaling_profile(
        agent, stages=stages, seed=4, tick_s=0.1,
        sleep=clock.sleep, monotonic=clock.monotonic,
        on_stage=lambda i, s: seen.append((i, s["target_qps"])),
    )
    assert seen == [(0, 2), (1, 4)]


def test_default_scaling_stages_are_monotonic_staircase():
    qps = [s["qps"] for s in SCALING_STAGES]
    assert qps == sorted(qps)  # non-decreasing staircase
    assert len(set(qps)) > 1  # actually scales up
    assert all(s["duration_s"] > 0 for s in SCALING_STAGES)
