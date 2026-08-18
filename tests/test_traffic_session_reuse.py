import threading

from src.traffic.generate_traffic import SessionPool


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
