"""Offline tests for the concurrent ramped load generator (no live GCP).

These exercise `generate_load` with a fake agent and injectable time/RNG so the
scheduler is deterministic and fast — only the concurrency-overlap test uses a
tiny amount of real wall-clock time.
"""

import threading
import time

import pytest

from src.armor.config import BLOCKED_PATTERNS
from src.traffic.generate_traffic import INJECTED_QUERIES, generate_load


class FakeAgent:
    """Minimal agent stub: create_session + a (fast) empty stream_query."""

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def create_session(self, user_id=None):
        return {"id": f"sess-{user_id}"}

    def stream_query(self, *, user_id, session_id, message):
        with self._lock:
            self.calls.append((user_id, message))
        return iter([{"text": "ok"}])


class ConcurrencyAgent:
    """Agent stub that records the max number of overlapping stream_query calls."""

    def __init__(self, hold_s=0.01):
        self.hold_s = hold_s
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def create_session(self, user_id=None):
        return {"id": "s"}

    def stream_query(self, *, user_id, session_id, message):
        # Generator body runs while the caller iterates, so "active" is held
        # for the duration of the tiny real sleep.
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.hold_s)
            yield {"text": "ok"}
        finally:
            with self._lock:
                self.active -= 1


class FakeClock:
    """Virtual clock: monotonic() reads t; sleep(s) advances t by s."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_concurrency_overlaps():
    """With workers>1 and a small real hold, calls must overlap."""
    agent = ConcurrencyAgent(hold_s=0.01)
    summary = generate_load(
        agent,
        target_qps=100,
        duration_s=0.3,
        ramp_s=0,
        workers=4,
        seed=1,
        tick_s=0.05,
    )
    assert agent.max_active > 1
    assert summary["sent"] > 1


def test_ramp_reaches_target():
    """Offered volume matches the ramp triangle + hold rectangle."""
    clock = FakeClock()
    agent = FakeAgent()
    target, duration, ramp = 20, 10.0, 4.0
    summary = generate_load(
        agent,
        target_qps=target,
        duration_s=duration,
        ramp_s=ramp,
        workers=8,
        seed=0,
        tick_s=0.1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    hold = duration - ramp
    expected = target * ramp / 2 + target * hold  # triangle + rectangle
    assert summary["offered"] == pytest.approx(expected, rel=0.1)
    assert summary["sent"] == summary["offered"]  # fake agent never fails


def test_error_injection_all_injected():
    clock = FakeClock()
    dispatched = []
    agent = FakeAgent()
    summary = generate_load(
        agent,
        target_qps=10,
        duration_s=3.0,
        ramp_s=0,
        workers=4,
        error_injection=1.0,
        seed=7,
        tick_s=0.1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        on_dispatch=lambda user, msg, inj: dispatched.append((msg, inj)),
    )
    assert summary["sent"] > 0
    assert summary["injected"] == summary["sent"]
    assert all(inj for _, inj in dispatched)
    assert all(msg in INJECTED_QUERIES for msg, _ in dispatched)


def test_no_injection():
    clock = FakeClock()
    dispatched = []
    agent = FakeAgent()
    summary = generate_load(
        agent,
        target_qps=10,
        duration_s=3.0,
        ramp_s=0,
        workers=4,
        error_injection=0.0,
        seed=7,
        tick_s=0.1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        on_dispatch=lambda user, msg, inj: dispatched.append((msg, inj)),
    )
    assert summary["injected"] == 0
    assert all(not inj for _, inj in dispatched)
    assert all(msg not in INJECTED_QUERIES for msg, _ in dispatched)


def _run_once(seed):
    clock = FakeClock()
    dispatched = []
    agent = FakeAgent()
    generate_load(
        agent,
        target_qps=15,
        duration_s=5.0,
        ramp_s=2.0,
        workers=8,
        error_injection=0.3,
        seed=seed,
        tick_s=0.1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        on_dispatch=lambda user, msg, inj: dispatched.append((user, msg)),
    )
    return dispatched


def test_determinism_same_seed():
    a = _run_once(123)
    b = _run_once(123)
    assert len(a) > 0
    assert a == b
    # Different seed almost certainly yields a different dispatch sequence.
    assert _run_once(999) != a


def test_injected_matches_blocked_pattern():
    matched = [q for q in INJECTED_QUERIES if any(p.search(q) for p in BLOCKED_PATTERNS)]
    assert matched, "at least one INJECTED_QUERIES entry must match a BLOCKED_PATTERNS regex"


def test_parse_labels_key_value_pairs():
    from src.traffic.generate_traffic import parse_labels

    assert parse_labels(["model=gemini-3.6-flash", "run=demo1"]) == {
        "model": "gemini-3.6-flash",
        "run": "demo1",
    }
    assert parse_labels(None) == {}
    assert parse_labels([]) == {}


def test_parse_labels_rejects_malformed():
    import pytest

    from src.traffic.generate_traffic import parse_labels

    with pytest.raises(ValueError):
        parse_labels(["no-equals-sign"])


def test_load_emit_metrics_applies_extra_labels():
    """--label plumbing: every emitted agent_traffic/* series carries extra_labels."""
    from src.observability.metrics import MetricsWriter
    from tests.test_metrics import FakeMetricClient

    clock = FakeClock()
    agent = FakeAgent()
    client = FakeMetricClient()
    writer = MetricsWriter(project_id="proj-x", client=client)
    generate_load(
        agent,
        target_qps=2,
        duration_s=3.0,
        workers=4,
        seed=0,
        tick_s=0.1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        emit_metrics=True,
        metrics_writer=writer,
        extra_labels={"model": "gemini-3.6-flash"},
    )
    series = client.flatten()
    assert series  # something was emitted
    for ts in series:
        assert ts.metric.labels["model"] == "gemini-3.6-flash"


def test_load_no_metrics_writer_used_only_when_emitting():
    """A provided writer stays untouched unless emit_metrics is on."""
    from src.observability.metrics import MetricsWriter
    from tests.test_metrics import FakeMetricClient

    clock = FakeClock()
    agent = FakeAgent()
    client = FakeMetricClient()
    writer = MetricsWriter(project_id="proj-x", client=client)
    generate_load(
        agent,
        target_qps=2,
        duration_s=2.0,
        seed=0,
        tick_s=0.1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        metrics_writer=writer,  # provided, but emit_metrics defaults False
    )
    assert client.calls == []
