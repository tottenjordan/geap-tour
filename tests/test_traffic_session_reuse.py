import threading

import src.traffic.generate_traffic as gt
from src.traffic.generate_traffic import SessionPool, _send_single_query
from tests.test_traffic_load import FakeClock


class CountingAgent:
    """Counts create_session calls; returns a per-user session id."""

    def __init__(self):
        self.create_calls = []
        self._lock = threading.Lock()

    def create_session(self, user_id=None):
        with self._lock:
            self.create_calls.append(user_id)
        return {"id": f"sess-{user_id}-{len(self.create_calls)}"}


def test_pool_reuses_one_session_per_user():
    agent = CountingAgent()
    pool = SessionPool(agent)
    sids = [pool.get("alice") for _ in range(5)]
    assert len(set(sids)) == 1  # same session every time
    assert agent.create_calls == ["alice"]  # created exactly once


def test_pool_one_session_per_distinct_user():
    agent = CountingAgent()
    pool = SessionPool(agent)
    pool.get("alice")
    pool.get("bob")
    pool.get("alice")
    pool.get("charlie")
    assert sorted(agent.create_calls) == ["alice", "bob", "charlie"]


def test_pool_invalidate_forces_recreate():
    agent = CountingAgent()
    pool = SessionPool(agent)
    first = pool.get("alice")
    pool.invalidate("alice")
    second = pool.get("alice")
    assert first != second
    assert agent.create_calls == ["alice", "alice"]


def test_pool_thread_safe_single_create_under_concurrency():
    agent = CountingAgent()
    pool = SessionPool(agent)
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()  # maximize contention
        pool.get("alice")

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert agent.create_calls == ["alice"]  # lock => created exactly once


class PoolAgent(CountingAgent):
    """create_session (counted) + a stream_query that records the session used."""

    def __init__(self):
        super().__init__()
        self.streamed = []  # (session_id, message)

    def stream_query(self, *, user_id, session_id, message):
        self.streamed.append((session_id, message))
        return iter([{"text": "ok"}])


def test_send_single_query_reuses_pool_session():
    agent = PoolAgent()
    pool = SessionPool(agent)
    for i in range(4):
        assert _send_single_query(agent, f"q{i}", "alice", "low", session_pool=pool)
    assert agent.create_calls == ["alice"]  # one session for 4 queries
    assert len({s for s, _ in agent.streamed}) == 1  # all on the same session


def test_send_single_query_without_pool_is_unchanged():
    agent = PoolAgent()
    for i in range(3):
        assert _send_single_query(agent, f"q{i}", "alice", "low")
    assert agent.create_calls == ["alice", "alice", "alice"]  # per-query (legacy)


class StaleOnceAgent(PoolAgent):
    """Only the FIRST distinct session id is stale; the recreated one works.

    Retry-once semantics: the first session that stream_query ever sees fails
    with a stale-session error; every later (recreated) session succeeds.
    """

    def __init__(self):
        super().__init__()
        self._stale_sid = None

    def stream_query(self, *, user_id, session_id, message):
        if self._stale_sid is None:
            self._stale_sid = session_id
            raise RuntimeError("404 Session not found or terminated")
        self.streamed.append((session_id, message))
        return iter([{"text": "ok"}])


def test_stale_session_invalidates_and_retries_once():
    agent = StaleOnceAgent()
    pool = SessionPool(agent)
    # First call: sid#1 fails stale -> invalidate -> sid#2 is created and succeeds.
    ok = _send_single_query(agent, "q", "alice", "low", session_pool=pool)
    assert ok is True
    assert len(agent.create_calls) == 2  # recreated once after the stale error
    assert len({s for s, _ in agent.streamed}) == 1  # served on the fresh session


def _patch_engine(monkeypatch, agent):
    """Stub out the cloud plumbing generate_steady_traffic uses to get an agent."""
    monkeypatch.setattr(gt.vertexai, "init", lambda **_: None)
    monkeypatch.setattr(
        gt, "_resolve_engine_resource", lambda a, d: "projects/x/reasoningEngines/1"
    )
    monkeypatch.setattr(gt.agent_engines, "get", lambda name: agent)


def test_steady_passes_a_session_pool(monkeypatch):
    agent = PoolAgent()
    _patch_engine(monkeypatch, agent)
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    # Advance the clock past end_time after the first batch so exactly one query
    # is sent, then the loop stops (first two time.time() calls seed end_time and
    # enter the loop; every later call is far past end_time).
    times = iter([0.0, 0.0])
    monkeypatch.setattr(gt.time, "time", lambda: next(times, 1_000_000.0))

    seen = {}

    def spy(a, q, u, c, *, session_pool=None):
        seen["pool"] = session_pool
        return True

    monkeypatch.setattr(gt, "_send_single_query", spy)
    gt.generate_steady_traffic(duration_minutes=1, interval_seconds=999, queries_per_interval=1)
    assert isinstance(seen["pool"], gt.SessionPool)


def test_generate_load_reuse_caps_create_sessions():
    agent = PoolAgent()
    clock = FakeClock()
    summary = gt.generate_load(
        agent,
        target_qps=5,
        duration_s=2.0,
        ramp_s=0,
        workers=4,
        user_pool=["alice", "bob", "charlie"],
        seed=1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        reuse_sessions=True,
    )
    assert summary["sent"] > 3
    # create_session called at most once per distinct user, NOT once per query:
    assert set(agent.create_calls) <= {"alice", "bob", "charlie"}
    assert len(agent.create_calls) <= 3


def test_generate_load_no_reuse_is_per_query():
    agent = PoolAgent()
    clock = FakeClock()
    summary = gt.generate_load(
        agent,
        target_qps=5,
        duration_s=2.0,
        ramp_s=0,
        workers=4,
        user_pool=["alice"],
        seed=1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        reuse_sessions=False,
    )
    assert len(agent.create_calls) == summary["sent"]  # one session per query


def test_scaling_shares_one_pool_across_stages():
    agent = PoolAgent()
    clock = FakeClock()
    stages = [{"qps": 3, "duration_s": 1.0}, {"qps": 3, "duration_s": 1.0}]
    gt.generate_scaling_profile(
        agent,
        stages=stages,
        workers=4,
        seed=1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    # Two stages but sessions persist across them -> still <= distinct users:
    assert len(agent.create_calls) <= 3


def test_cli_steady_no_reuse_flag_forwards_false(monkeypatch):
    seen = {}
    monkeypatch.setattr(gt, "generate_steady_traffic", lambda **kw: seen.update(kw))
    gt.main(["ENG", "--steady", "--no-reuse-sessions", "--duration", "1"])
    assert seen["reuse_sessions"] is False


def test_cli_steady_reuse_defaults_true(monkeypatch):
    seen = {}
    monkeypatch.setattr(gt, "generate_steady_traffic", lambda **kw: seen.update(kw))
    gt.main(["ENG", "--steady", "--duration", "1"])
    assert seen["reuse_sessions"] is True


def test_cli_load_no_reuse_flag_forwards_false(monkeypatch):
    agent = PoolAgent()
    _patch_engine(monkeypatch, agent)
    monkeypatch.setattr(gt, "disable_pyopenssl", lambda: None)
    seen = {}
    monkeypatch.setattr(gt, "generate_load", lambda *a, **kw: seen.update(kw))
    gt.main(["ENG", "--load", "--no-reuse-sessions", "--duration", "1"])
    assert seen["reuse_sessions"] is False
