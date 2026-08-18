import threading

from src.traffic.generate_traffic import SessionPool, _send_single_query


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
