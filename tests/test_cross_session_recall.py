import pytest


class TestRecallSurvivesTheSseParseSkew:
    """`verify_cross_session_recall` was the ONE live-streaming module without the
    raw-SSE fallback, and it is a check `demo_readiness` marks critical.

    A recycled-but-healthy engine streams NDJSON that google-api-core's array-only
    parser rejects with `ValueError: Can only parse array of JSON objects`. Nine
    other modules absorb this via `src/eval/raw_stream.py`; this one raised, so a
    perfectly healthy engine reported `DEMO READINESS: NOT READY`. Observed live on
    2026-09-09 against probe engine 4380... while `engine_live` passed on the same
    engine in the same run — which is exactly how you can tell it is a parser skew
    and not a broken agent.

    The fallback MUST preserve `session_id`: cross-session recall is defined by
    asking in a brand-new session B, so a fallback that minted its own session
    would silently test nothing.
    """

    @staticmethod
    def _skew():
        return ValueError("Can only parse array of JSON objects, instead got {")

    def test_the_skew_is_absorbed_not_raised(self, monkeypatch):
        from src.eval import raw_stream
        from src.eval import verify_cross_session_recall as vcsr

        class Agent:
            api_resource = type("R", (), {"name": "projects/p/locations/l/reasoningEngines/9"})()

            def stream_query(self, **_kw):
                raise TestRecallSurvivesTheSseParseSkew._skew()

        captured = {}

        def fake_events(resource, *, message, user_id, session_id, **_kw):
            captured.update(resource=resource, session_id=session_id, user_id=user_id)
            return [{"content": {"parts": [{"text": "You prefer window seats."}]}}]

        monkeypatch.setattr(raw_stream, "stream_query_events", fake_events)
        out = vcsr._drain_stream(Agent(), user_id="alice", session_id="SESSION-B", message="hi")
        assert "window" in out
        assert captured["session_id"] == "SESSION-B", (
            "the fallback must reuse the caller's session — a new one would make "
            "the cross-session test vacuous"
        )

    def test_a_real_error_still_propagates(self, monkeypatch):
        """Only the parser skew is absorbed. A genuine failure must stay loud."""
        from src.eval import verify_cross_session_recall as vcsr

        class Agent:
            api_resource = type("R", (), {"name": "projects/p/locations/l/reasoningEngines/9"})()

            def stream_query(self, **_kw):
                raise ValueError("engine exploded")

        with pytest.raises(ValueError, match="engine exploded"):
            vcsr._drain_stream(Agent(), user_id="alice", session_id="s", message="hi")

    def test_the_happy_path_is_unchanged(self):
        from src.eval import verify_cross_session_recall as vcsr

        class Agent:
            def stream_query(self, **_kw):
                return [{"content": {"parts": [{"text": "ok"}]}}]

        assert vcsr._drain_stream(Agent(), user_id="a", session_id="s", message="m") == "ok"
