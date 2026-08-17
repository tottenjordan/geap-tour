"""Offline tests for the cross-session Memory Bank recall driver.

The driver proves genuine cross-session recall: a preference stated in session A
is surfaced in a *separate* session B for the same user. All engine/store calls
are faked — no live GCP.
"""

import pytest

from src.eval import verify_cross_session_recall as xr


class FakeAgent:
    """Records session/query calls; yields canned chunks.

    ``create_session`` hands out incrementing ids (so A != B). ``stream_query``
    logs every ``(user_id, session_id, message)`` and yields a chunk whose text
    is ``responses.get(session_id, default_response)`` — so the probe (session B)
    can be given a recall-bearing answer while the seeds (session A) stay quiet.
    """

    def __init__(self, responses=None, default_response=""):
        self.responses = responses or {}
        self.default_response = default_response
        self._n = 0
        self.sessions_created = []
        self.queries = []

    def create_session(self, user_id=None):
        self._n += 1
        sid = f"sess-{self._n}"
        self.sessions_created.append((user_id, sid))
        return {"id": sid}

    def stream_query(self, user_id=None, session_id=None, message=None):
        self.queries.append((user_id, session_id, message))
        text = self.responses.get(session_id, self.default_response)
        yield {"content": {"parts": [{"text": text}]}}


def _no_sleep(_seconds):  # deterministic sleep stub
    raise AssertionError("sleep_fn should not be called when facts are already present")


def test_creates_two_distinct_sessions_and_routes_turns(monkeypatch):
    agent = FakeAgent(responses={"sess-2": "Booked a window seat on Delta."})
    monkeypatch.setattr(xr, "fetch_memories", lambda *a, **k: ["Prefers window/Delta"])

    result = xr.run_cross_session_recall(
        "alice", agent=agent, seed_messages=["s1", "s2"], sleep_fn=_no_sleep
    )

    assert result["session_a_id"] == "sess-1"
    assert result["session_b_id"] == "sess-2"
    assert result["session_a_id"] != result["session_b_id"]
    # Seeds went to session A; the single probe went to session B.
    a_msgs = [m for (_, sid, m) in agent.queries if sid == "sess-1"]
    b_msgs = [m for (_, sid, m) in agent.queries if sid == "sess-2"]
    assert a_msgs == ["s1", "s2"]
    assert len(b_msgs) == 1


def test_recalled_true_when_signal_present(monkeypatch):
    agent = FakeAgent(responses={"sess-2": "Sure — a WINDOW seat, as you like."})
    monkeypatch.setattr(xr, "fetch_memories", lambda *a, **k: ["x"])
    result = xr.run_cross_session_recall(
        "alice", agent=agent, expected_signals=["window"], sleep_fn=_no_sleep
    )
    assert result["recalled"] is True


def test_recalled_false_when_signal_absent(monkeypatch):
    agent = FakeAgent(responses={"sess-2": "I need more details to book that."})
    monkeypatch.setattr(xr, "fetch_memories", lambda *a, **k: ["x"])
    result = xr.run_cross_session_recall(
        "alice", agent=agent, expected_signals=["window", "Delta"], sleep_fn=_no_sleep
    )
    assert result["recalled"] is False


def test_probe_retries_on_empty_stream(monkeypatch):
    """An empty probe stream (cold-start empty-at-200) retries in a fresh session."""
    # sess-2 (first probe) is empty; sess-3 (retry) carries the recall.
    agent = FakeAgent(responses={"sess-3": "A window seat, as you prefer."}, default_response="")
    monkeypatch.setattr(xr, "fetch_memories", lambda *a, **k: ["x"])

    result = xr.run_cross_session_recall(
        "alice", agent=agent, expected_signals=["window"], sleep_fn=_no_sleep
    )

    assert result["recalled"] is True
    assert result["session_b_id"] == "sess-3"  # advanced to the retry session
    assert result["probe_response"] == "A window seat, as you prefer."


def test_probe_attempts_one_disables_retry(monkeypatch):
    agent = FakeAgent(responses={"sess-3": "window"}, default_response="")
    monkeypatch.setattr(xr, "fetch_memories", lambda *a, **k: ["x"])

    result = xr.run_cross_session_recall(
        "alice", agent=agent, expected_signals=["window"], probe_attempts=1, sleep_fn=_no_sleep
    )

    assert result["session_b_id"] == "sess-2"  # no retry
    assert result["recalled"] is False  # empty probe → no signal


def test_poll_waits_until_facts_appear(monkeypatch):
    agent = FakeAgent(responses={"sess-2": "window"})
    # First two polls empty, third returns facts -> sleep called exactly twice.
    calls = {"n": 0}

    def fake_fetch(*a, **k):
        calls["n"] += 1
        return [] if calls["n"] < 3 else ["Prefers window seats"]

    sleeps = []
    monkeypatch.setattr(xr, "fetch_memories", fake_fetch)
    result = xr.run_cross_session_recall(
        "alice",
        agent=agent,
        expected_signals=["window"],
        poll_timeout_s=100.0,
        poll_interval_s=10.0,
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert result["facts"] == ["Prefers window seats"]
    assert sleeps == [10.0, 10.0]  # two empty polls before facts appeared


def test_poll_gives_up_at_timeout(monkeypatch):
    agent = FakeAgent(responses={"sess-2": "window"})
    monkeypatch.setattr(xr, "fetch_memories", lambda *a, **k: [])
    sleeps = []
    result = xr.run_cross_session_recall(
        "alice",
        agent=agent,
        expected_signals=["window"],
        poll_timeout_s=30.0,
        poll_interval_s=10.0,
        sleep_fn=lambda s: sleeps.append(s),
    )
    assert result["facts"] == []
    assert len(sleeps) == 3  # 10, 20, 30 then stop
    # Recall can still pass off the probe even when the store read timed out.
    assert result["recalled"] is True


def test_no_wait_skips_polling(monkeypatch):
    agent = FakeAgent(responses={"sess-2": "window"})

    def boom(*a, **k):
        raise AssertionError("fetch_memories must not be called when wait=False")

    monkeypatch.setattr(xr, "fetch_memories", boom)
    result = xr.run_cross_session_recall(
        "alice", agent=agent, expected_signals=["window"], wait=False
    )
    assert result["facts"] == []
    assert result["recalled"] is True


def test_raises_if_session_b_equals_session_a(monkeypatch):
    class StuckAgent(FakeAgent):
        def create_session(self, user_id=None):
            self.sessions_created.append((user_id, "sess-same"))
            return {"id": "sess-same"}

    monkeypatch.setattr(xr, "fetch_memories", lambda *a, **k: ["x"])
    with pytest.raises(RuntimeError, match="cross-session"):
        xr.run_cross_session_recall(
            "alice", agent=StuckAgent(), expected_signals=["window"], sleep_fn=_no_sleep
        )


def test_main_exit_codes(monkeypatch):
    def fake_run(user_id, **kwargs):
        return {
            "recalled": user_id == "yes",
            "session_a_id": "sess-1",
            "session_b_id": "sess-2",
            "facts": ["Prefers window seats"],
            "probe_response": "A window seat on Delta.",
        }

    monkeypatch.setattr(xr, "run_cross_session_recall", fake_run)
    assert xr.main(["--user-id", "yes"]) == 0
    assert xr.main(["--user-id", "no"]) == 1
