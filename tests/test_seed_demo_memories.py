"""Offline tests for the curated Memory Bank demo-seeding driver.

The driver writes each persona's facts directly to Memory Bank via
``create_memory`` (synchronous, no async distillation) then reads them back to
confirm. All engine/store calls are faked — no live GCP.
"""

import pytest

from src.eval import seed_demo_memories as sd


class FakeMemories:
    """Records create_memory calls and serves them back via fetch_memories path."""

    def __init__(self):
        # (user_id) -> list[fact]
        self.store: dict[str, list[str]] = {}
        self.creates: list[tuple[str, str, dict]] = []

    def create_memory(self, *, name, fact, scope):
        self.creates.append((name, fact, scope))
        self.store.setdefault(scope["user_id"], []).append(fact)


class FakeAgentEngines:
    def __init__(self, memories):
        self._memories = memories

    def create_memory(self, *, name, fact, scope):
        self._memories.create_memory(name=name, fact=fact, scope=scope)

    def retrieve_memories(self, *, name, scope, simple_retrieval_params=None):
        for fact in self._memories.store.get(scope["user_id"], []):
            yield type("M", (), {"fact": fact})()


class FakeClient:
    def __init__(self):
        self.memories = FakeMemories()
        self.agent_engines = FakeAgentEngines(self.memories)


def _persona(uid="alice", facts=("f0", "f1")):
    return sd.Persona(user_id=uid, facts=tuple(facts), signals=("x",))


def test_create_persona_memories_writes_each_fact():
    client = FakeClient()
    created = sd.create_persona_memories(client, _persona("alice", ("a", "b")), engine_id="123")
    assert created == ["a", "b"]
    assert [f for (_, f, _) in client.memories.creates] == ["a", "b"]
    # scope carries user_id + the engine-id app_name (the runtime's scope)
    _, _, scope = client.memories.creates[0]
    assert scope["user_id"] == "alice"
    assert scope["app_name"] == "123"


def test_create_persona_memories_scopes_by_full_name_engine_id():
    """A full resource name is reduced to the bare engine id for the app_name scope."""
    client = FakeClient()
    sd.create_persona_memories(
        client,
        _persona("alice", ("a",)),
        engine_id="projects/p/locations/us-central1/reasoningEngines/999",
    )
    _, _, scope = client.memories.creates[0]
    assert scope["app_name"] == "999"


def test_create_persona_memories_is_idempotent():
    client = FakeClient()
    p = _persona("alice", ("a", "b"))
    sd.create_persona_memories(client, p, engine_id="123")
    again = sd.create_persona_memories(client, p, engine_id="123")
    assert again == []  # both facts already present → nothing new
    assert client.memories.store["alice"] == ["a", "b"]  # no duplicates


def test_run_seed_creates_and_confirms_all_personas():
    client = FakeClient()
    personas = (_persona("alice", ("a",)), _persona("dana", ("b",)))
    results = sd.run_seed(personas, client=client, engine_id="123")
    assert [r["user_id"] for r in results] == ["alice", "dana"]
    assert all(r["seeded"] for r in results)
    assert results[0]["facts"] == ["a"]
    assert results[0]["n_facts"] == 1
    assert results[0]["created"] == ["a"]


def test_run_seed_no_verify_skips_readback(monkeypatch):
    """verify=False does no read-back — only the idempotency fetch runs (1 per persona)."""
    client = FakeClient()
    calls = {"n": 0}
    orig = sd.fetch_memories

    def counting_fetch(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(sd, "fetch_memories", counting_fetch)

    results = sd.run_seed(
        (_persona("alice", ("a",)),), client=client, engine_id="123", verify=False
    )
    assert results[0]["seeded"] is True
    assert results[0]["facts"] == ["a"]  # created facts, not a read-back
    assert calls["n"] == 1  # idempotency check only; no confirmation read-back


def test_run_seed_verify_does_readback(monkeypatch):
    """verify=True adds a confirmation read-back on top of the idempotency fetch."""
    client = FakeClient()
    calls = {"n": 0}
    orig = sd.fetch_memories

    def counting_fetch(*a, **k):
        calls["n"] += 1
        return orig(*a, **k)

    monkeypatch.setattr(sd, "fetch_memories", counting_fetch)

    sd.run_seed((_persona("alice", ("a",)),), client=client, engine_id="123", verify=True)
    assert calls["n"] == 2  # idempotency check + confirmation read-back


def test_select_personas_filters_by_user():
    got = sd._select_personas(["alice", "dana"])
    assert {p.user_id for p in got} == {"alice", "dana"}
    assert sd._select_personas(None) == list(sd.DEMO_PERSONAS)


def test_main_exit_codes(monkeypatch):
    def _rows(personas, seeded):
        return [
            {
                "user_id": p.user_id,
                "created": ["f"] if seeded else [],
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


def test_alice_facts_contain_expected_signals():
    """The literal facts (not just signal labels) mention each recall signal."""
    from src.eval import verify_cross_session_recall as xr

    alice = next(p for p in sd.DEMO_PERSONAS if p.user_id == "alice")
    blob = " ".join(alice.facts).lower()
    for sig in xr.DEFAULT_EXPECTED_SIGNALS:
        assert sig.lower() in blob
