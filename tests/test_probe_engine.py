"""Tests for the stream-probe CLI — honest empty-stream detection.

The coordinator-outage signature is an engine that returns HTTP 200 with **zero
events and no exception**. Existing tools (``generate_traffic``) count that as a
success, so ``probe_engine`` exists to report an honest PASS/FAIL on event count.
These tests use a fake engine (no GCP).
"""

from src.eval.probe_engine import format_result, main, probe_engine


class _FakeEngine:
    """Minimal stand-in for a deployed Agent Engine.

    ``events`` is the list yielded by ``stream_query``; ``raise_exc`` makes the
    stream raise (a hard failure, distinct from an empty stream).
    """

    def __init__(self, events=None, *, raise_exc=None):
        self._events = events or []
        self._raise_exc = raise_exc
        self.created_sessions = 0

    def create_session(self, *, user_id):
        self.created_sessions += 1
        return {"id": f"session-for-{user_id}"}

    def stream_query(self, *, user_id, session_id, message):
        if self._raise_exc is not None:
            raise self._raise_exc

        yield from self._events


def _text_event(text):
    return {"content": {"parts": [{"text": text}]}}


def _thought_event(text):
    return {"content": {"parts": [{"text": text, "thought": True}]}}


class TestProbeEngine:
    def test_streaming_engine_passes(self):
        engine = _FakeEngine([_text_event("hi"), _text_event(" there"), _thought_event("...")])
        result = probe_engine(engine, "find a flight")
        assert result["events"] == 3
        assert result["ok"] is True
        assert result["error"] is None
        assert engine.created_sessions == 1

    def test_empty_stream_fails_without_error(self):
        # The outage signature: 200, zero events, no exception.
        engine = _FakeEngine([])
        result = probe_engine(engine, "find a flight")
        assert result["events"] == 0
        assert result["ok"] is False
        assert result["error"] is None

    def test_stream_exception_is_captured_not_raised(self):
        engine = _FakeEngine(raise_exc=RuntimeError("boom"))
        result = probe_engine(engine, "find a flight")
        assert result["ok"] is False
        assert result["events"] == 0
        assert "boom" in result["error"]

    def test_text_events_counts_only_visible_text(self):
        # Two visible-text events + one thought-only event → text_events == 2.
        engine = _FakeEngine([_text_event("a"), _thought_event("b"), _text_event("c")])
        result = probe_engine(engine, "q")
        assert result["events"] == 3
        assert result["text_events"] == 2

    def test_reuses_given_session_id(self):
        engine = _FakeEngine([_text_event("x")])
        result = probe_engine(engine, "q", session_id="preexisting")
        assert result["ok"] is True
        # A caller-supplied session id means no new session is created.
        assert engine.created_sessions == 0

    def test_timing_fields_present(self):
        engine = _FakeEngine([_text_event("x")])
        result = probe_engine(engine, "q")
        assert "elapsed_s" in result
        assert result["elapsed_s"] >= 0


class TestFormatResult:
    def test_pass_summary(self):
        line = format_result(
            {"events": 3, "text_events": 2, "ok": True, "error": None, "elapsed_s": 1.2}
        )
        assert "PASS" in line
        assert "3" in line

    def test_fail_summary_empty_stream(self):
        line = format_result(
            {"events": 0, "text_events": 0, "ok": False, "error": None, "elapsed_s": 0.5}
        )
        assert "FAIL" in line

    def test_fail_summary_includes_error(self):
        line = format_result(
            {
                "events": 0,
                "text_events": 0,
                "ok": False,
                "error": "RuntimeError: boom",
                "elapsed_s": 0.0,
            }
        )
        assert "FAIL" in line
        assert "boom" in line


class TestMainCLI:
    def test_exit_zero_when_streaming(self):
        engine = _FakeEngine([_text_event("hi")])
        code = main(["12345"], get_engine=lambda _resource: engine)
        assert code == 0

    def test_exit_one_on_empty_stream(self):
        engine = _FakeEngine([])
        code = main(["12345"], get_engine=lambda _resource: engine)
        assert code == 1

    def test_resource_passed_through(self):
        engine = _FakeEngine([_text_event("hi")])
        seen = {}

        def _get(resource):
            seen["resource"] = resource
            return engine

        main(["projects/p/locations/us-central1/reasoningEngines/42"], get_engine=_get)
        assert seen["resource"] == "projects/p/locations/us-central1/reasoningEngines/42"
