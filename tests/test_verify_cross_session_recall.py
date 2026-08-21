"""Offline tests for the cross-session Memory Bank recall driver.

The driver proves genuine cross-session recall: a preference stated in session A
is surfaced in a *separate* session B for the same user. All engine/store calls
are faked — no live GCP.
"""

from typing import ClassVar

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


def _judge(recalled: bool, reason: str = "canned"):
    """A fake judge returning the contract lines the real parser reads."""
    return lambda _prompt: f"Recalled: {'yes' if recalled else 'no'}\nReason: {reason}"


class TestEvaluateRecall:
    """The verdict logic, isolated from the engine and the store.

    This replaced a substring check (``any(sig in response)``) that could not tell
    recall from its exact opposite.
    """

    FACTS: ClassVar[list[str]] = [
        "Prefers window seats",
        "Flies Delta",
        "Marriott corporate rate",
    ]

    def test_a_negation_containing_a_signal_word_is_not_recall(self):
        """THE BUG. "I don't have a saved WINDOW preference" contains 'window', so
        the old substring check passed on the exact symptom of memory being broken
        — and this check is *critical* in `demo_readiness --deep`."""
        verdict = xr.evaluate_recall(
            "I don't have a saved window seat preference on file.",
            facts=self.FACTS,
            generate_fn=_judge(False, "the reply denies holding the fact"),
        )
        assert verdict["recalled"] is False
        assert "denies" in verdict["reason"]

    def test_an_affirmative_recall_passes(self):
        verdict = xr.evaluate_recall(
            "You prefer window seats and usually fly Delta.",
            facts=self.FACTS,
            generate_fn=_judge(True, "surfaces two stored preferences"),
        )
        assert verdict["recalled"] is True

    def test_a_paraphrase_without_the_signal_word_still_passes(self):
        """The other side of the substring check: recall phrased differently used
        to FAIL. The judge sees meaning, so it passes."""
        verdict = xr.evaluate_recall(
            "You like a seat by the glass, and you're loyal to one carrier.",
            facts=self.FACTS,
            generate_fn=_judge(True, "paraphrases the stored seat preference"),
        )
        assert verdict["recalled"] is True

    def test_an_empty_response_fails_without_calling_the_judge(self):
        def explode(_prompt):
            raise AssertionError("must not spend a judge call on an empty stream")

        verdict = xr.evaluate_recall("   ", facts=self.FACTS, generate_fn=explode)
        assert verdict["recalled"] is False
        assert "empty" in verdict["reason"].lower()

    def test_a_judge_error_fails_loudly_and_never_falls_back(self):
        """Falling back to the substring check on a judge error would quietly
        reintroduce the bug this whole change exists to remove."""

        def boom(_prompt):
            raise RuntimeError("judge 503")

        verdict = xr.evaluate_recall("You prefer window seats.", facts=self.FACTS, generate_fn=boom)
        assert verdict["recalled"] is False
        assert "judge" in verdict["reason"].lower()

    def test_an_unparseable_verdict_is_not_recall(self):
        verdict = xr.evaluate_recall(
            "You prefer window seats.",
            facts=self.FACTS,
            generate_fn=lambda _p: "I have opinions but no verdict line",
        )
        assert verdict["recalled"] is False

    def test_the_prompt_carries_the_real_persisted_facts(self):
        """Grounding is the point — the judge compares against what the store
        actually holds, not against hardcoded demo signals."""
        seen = {}

        def capture(prompt):
            seen["prompt"] = prompt
            return "Recalled: yes"

        xr.evaluate_recall("You like window seats.", facts=self.FACTS, generate_fn=capture)
        for fact in self.FACTS:
            assert fact in seen["prompt"]


class TestParseRecallVerdict:
    def test_reads_yes_and_no(self):
        assert xr.parse_recall_verdict("Recalled: yes")["recalled"] is True
        assert xr.parse_recall_verdict("Recalled: no")["recalled"] is False

    def test_tolerates_markdown_and_case(self):
        assert xr.parse_recall_verdict("**Recalled:** YES")["recalled"] is True

    def test_uses_the_last_verdict_line(self):
        """Judges restate the criterion while reasoning; the final line is the
        contract (same convention as parse_faithfulness_score)."""
        text = "Recalled: no would be wrong here.\nRecalled: yes"
        assert xr.parse_recall_verdict(text)["recalled"] is True

    def test_missing_verdict_is_not_recall(self):
        assert xr.parse_recall_verdict("no verdict here")["recalled"] is False
        assert xr.parse_recall_verdict(None)["recalled"] is False

    def test_captures_the_reason(self):
        v = xr.parse_recall_verdict("Recalled: no\nReason: the reply asks for the info")
        assert "asks for the info" in v["reason"]


def test_creates_two_distinct_sessions_and_routes_turns(monkeypatch):
    agent = FakeAgent(responses={"sess-2": "Booked a window seat on Delta."})
    monkeypatch.setattr(xr, "fetch_memories", lambda *a, **k: ["Prefers window/Delta"])

    result = xr.run_cross_session_recall(
        "alice",
        agent=agent,
        seed_messages=["s1", "s2"],
        sleep_fn=_no_sleep,
        generate_fn=_judge(True),
    )

    assert result["session_a_id"] == "sess-1"
    assert result["session_b_id"] == "sess-2"
    assert result["session_a_id"] != result["session_b_id"]
    # Seeds went to session A; the single probe went to session B.
    a_msgs = [m for (_, sid, m) in agent.queries if sid == "sess-1"]
    b_msgs = [m for (_, sid, m) in agent.queries if sid == "sess-2"]
    assert a_msgs == ["s1", "s2"]
    assert len(b_msgs) == 1


def test_recalled_true_when_the_judge_confirms(monkeypatch):
    agent = FakeAgent(responses={"sess-2": "Sure — a WINDOW seat, as you like."})
    monkeypatch.setattr(xr, "fetch_memories", lambda *a, **k: ["x"])
    result = xr.run_cross_session_recall(
        "alice", agent=agent, sleep_fn=_no_sleep, generate_fn=_judge(True)
    )
    assert result["recalled"] is True


def test_recalled_false_when_the_judge_refuses(monkeypatch):
    agent = FakeAgent(responses={"sess-2": "I need more details to book that."})
    monkeypatch.setattr(xr, "fetch_memories", lambda *a, **k: ["x"])
    result = xr.run_cross_session_recall(
        "alice", agent=agent, sleep_fn=_no_sleep, generate_fn=_judge(False)
    )
    assert result["recalled"] is False


def test_signals_are_reported_but_do_not_decide(monkeypatch):
    """The old contract inverted: the signal word is present AND the judge says no
    (a denial naming the topic), so the verdict must be FAIL."""
    agent = FakeAgent(responses={"sess-2": "I have no window seat preference saved."})
    monkeypatch.setattr(xr, "fetch_memories", lambda *a, **k: ["x"])
    result = xr.run_cross_session_recall(
        "alice",
        agent=agent,
        expected_signals=["window"],
        sleep_fn=_no_sleep,
        generate_fn=_judge(False, "denies holding the preference"),
    )
    assert result["signals_found"] == ["window"]
    assert result["recalled"] is False


def test_probe_retries_on_empty_stream(monkeypatch):
    """An empty probe stream (cold-start empty-at-200) retries in a fresh session."""
    # sess-2 (first probe) is empty; sess-3 (retry) carries the recall.
    agent = FakeAgent(responses={"sess-3": "A window seat, as you prefer."}, default_response="")
    monkeypatch.setattr(xr, "fetch_memories", lambda *a, **k: ["x"])

    result = xr.run_cross_session_recall(
        "alice", agent=agent, sleep_fn=_no_sleep, generate_fn=_judge(True)
    )

    assert result["recalled"] is True
    assert result["session_b_id"] == "sess-3"  # advanced to the retry session
    assert result["probe_response"] == "A window seat, as you prefer."


def test_probe_attempts_one_disables_retry(monkeypatch):
    agent = FakeAgent(responses={"sess-3": "window"}, default_response="")
    monkeypatch.setattr(xr, "fetch_memories", lambda *a, **k: ["x"])

    result = xr.run_cross_session_recall(
        "alice", agent=agent, probe_attempts=1, sleep_fn=_no_sleep, generate_fn=_judge(True)
    )

    assert result["session_b_id"] == "sess-2"  # no retry
    # Even a judge that would say yes cannot rescue an empty stream.
    assert result["recalled"] is False


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
        poll_timeout_s=100.0,
        poll_interval_s=10.0,
        sleep_fn=lambda s: sleeps.append(s),
        generate_fn=_judge(True),
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
        poll_timeout_s=30.0,
        poll_interval_s=10.0,
        sleep_fn=lambda s: sleeps.append(s),
        generate_fn=_judge(True),
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
    result = xr.run_cross_session_recall("alice", agent=agent, wait=False, generate_fn=_judge(True))
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
            "alice", agent=StuckAgent(), sleep_fn=_no_sleep, generate_fn=_judge(True)
        )


def test_main_exit_codes(monkeypatch):
    def fake_run(user_id, **kwargs):
        return {
            "recalled": user_id == "yes",
            "session_a_id": "sess-1",
            "session_b_id": "sess-2",
            "facts": ["Prefers window seats"],
            "probe_response": "A window seat on Delta.",
            "reason": "surfaces the stored seat preference",
            "signals_found": ["window"],
        }

    monkeypatch.setattr(xr, "run_cross_session_recall", fake_run)
    assert xr.main(["--user-id", "yes"]) == 0
    assert xr.main(["--user-id", "no"]) == 1


def test_main_prints_the_judges_reason(monkeypatch, capsys):
    """A bare PASS/FAIL was unactionable; the reason says *why* recall failed."""

    def fake_run(user_id, **kwargs):
        return {
            "recalled": False,
            "session_a_id": "sess-1",
            "session_b_id": "sess-2",
            "facts": [],
            "probe_response": "I don't have that on file.",
            "reason": "the reply denies holding the fact",
            "signals_found": [],
        }

    monkeypatch.setattr(xr, "run_cross_session_recall", fake_run)
    assert xr.main(["--user-id", "alice"]) == 1
    out = capsys.readouterr().out
    assert "RECALL: FAIL — the reply denies holding the fact" in out
