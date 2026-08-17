"""Offline tests for the curated Memory Bank demo-seeding driver.

The driver drives short preference-stating sessions per persona (so the deployed
coordinator's ``save_memories_callback`` fires) then polls Memory Bank until facts
appear. All engine/store calls are faked — no live GCP.
"""

import pytest

from src.eval import seed_demo_memories as sd


class FakeAgent:
    """Hands out incrementing session ids and logs every stream_query turn."""

    def __init__(self):
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
        yield {"content": {"parts": [{"text": "ok"}]}}


def _persona(uid="alice", n=2):
    return sd.Persona(user_id=uid, messages=tuple(f"m{i}" for i in range(n)), signals=("x",))


def test_seed_persona_creates_session_and_sends_all_turns():
    agent = FakeAgent()
    sid = sd.seed_persona(agent, _persona("alice", 2))
    assert sid == "sess-1"
    assert agent.sessions_created == [("alice", "sess-1")]
    msgs = [m for (_, s, m) in agent.queries if s == "sess-1"]
    assert msgs == ["m0", "m1"]


def test_run_seed_seeds_all_personas_and_returns_facts(monkeypatch):
    agent = FakeAgent()
    personas = (_persona("alice", 1), _persona("dana", 1))
    monkeypatch.setattr(sd, "_poll_for_facts", lambda uid, **k: [f"{uid} fact"])

    results = sd.run_seed(personas, agent=agent)

    assert [r["user_id"] for r in results] == ["alice", "dana"]
    assert [r["session_id"] for r in results] == ["sess-1", "sess-2"]
    assert all(r["seeded"] for r in results)
    assert results[0]["n_facts"] == 1
    assert results[0]["facts"] == ["alice fact"]


def test_run_seed_seeds_all_before_polling(monkeypatch):
    """Every persona's session is created before any poll begins (async-friendly)."""
    agent = FakeAgent()
    seen = []

    def fake_poll(uid, **k):
        seen.append(len(agent.sessions_created))
        return ["f"]

    monkeypatch.setattr(sd, "_poll_for_facts", fake_poll)
    sd.run_seed((_persona("alice", 1), _persona("dana", 1)), agent=agent)

    # By the first poll, both sessions already exist.
    assert seen[0] == 2


def test_run_seed_timeout_marks_not_seeded(monkeypatch):
    agent = FakeAgent()
    monkeypatch.setattr(sd, "_poll_for_facts", lambda uid, **k: [])
    results = sd.run_seed((_persona("alice", 1),), agent=agent)
    assert results[0]["seeded"] is False
    assert results[0]["n_facts"] == 0


def test_run_seed_no_wait_skips_polling(monkeypatch):
    agent = FakeAgent()

    def boom(*a, **k):
        raise AssertionError("_poll_for_facts must not run when wait=False")

    monkeypatch.setattr(sd, "_poll_for_facts", boom)
    results = sd.run_seed((_persona("alice", 1),), agent=agent, wait=False)
    assert results[0]["seeded"] is True
    assert results[0]["facts"] == []


def test_select_personas_filters_by_user():
    got = sd._select_personas(["alice", "dana"])
    assert {p.user_id for p in got} == {"alice", "dana"}
    assert sd._select_personas(None) == list(sd.DEMO_PERSONAS)


def test_main_exit_codes(monkeypatch):
    def _rows(personas, seeded):
        return [
            {
                "user_id": p.user_id,
                "session_id": "s",
                "facts": ["f"] if seeded else [],
                "n_facts": 1 if seeded else 0,
                "seeded": seeded,
            }
            for p in personas
        ]

    monkeypatch.setattr(sd, "run_seed", lambda personas, **k: _rows(personas, True))
    assert sd.main(["--user", "alice"]) == 0

    monkeypatch.setattr(sd, "run_seed", lambda personas, **k: _rows(personas, False))
    assert sd.main(["--user", "alice"]) == 1


def test_main_dry_run_makes_no_calls(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry-run must not call run_seed")

    monkeypatch.setattr(sd, "run_seed", boom)
    assert sd.main(["--dry-run", "--engine-id", "123"]) == 0


def test_main_unknown_user_returns_error(monkeypatch):
    monkeypatch.setattr(sd, "run_seed", lambda *a, **k: pytest.fail("should not seed"))
    assert sd.main(["--user", "nobody"]) == 1


def test_alice_signals_match_recall_defaults():
    """alice's seeded facts must satisfy the cross-session recall demo's signals."""
    from src.eval import verify_cross_session_recall as xr

    alice = next(p for p in sd.DEMO_PERSONAS if p.user_id == "alice")
    expected = {s.lower() for s in xr.DEFAULT_EXPECTED_SIGNALS}
    assert expected <= {s.lower() for s in alice.signals}
