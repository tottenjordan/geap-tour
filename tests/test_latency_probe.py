"""Unit tests for latency_probe pure helpers (no GCP, no network)."""

from src.eval import latency_probe


def _event(*parts):
    return {"content": {"parts": list(parts)}}


class TestClassify:
    def test_function_response_is_response(self):
        ev = _event({"function_response": {"name": "search_flights"}})
        assert latency_probe._classify(ev) == "response"

    def test_function_call_is_call(self):
        ev = _event({"function_call": {"name": "search_flights"}})
        assert latency_probe._classify(ev) == "call"

    def test_text_is_text(self):
        ev = _event({"text": "here are your flights"})
        assert latency_probe._classify(ev) == "text"

    def test_response_wins_over_text(self):
        ev = _event({"text": "x"}, {"function_response": {"name": "book_flight"}})
        assert latency_probe._classify(ev) == "response"

    def test_empty_is_other(self):
        assert latency_probe._classify({}) == "other"
        assert latency_probe._classify(_event()) == "other"


class TestCallNames:
    def test_extracts_call_names(self):
        ev = _event(
            {"function_call": {"name": "search_flights"}},
            {"function_call": {"name": "book_flight"}},
        )
        assert latency_probe._call_names(ev) == ["search_flights", "book_flight"]

    def test_ignores_non_calls(self):
        ev = _event({"text": "x"}, {"function_response": {"name": "y"}})
        assert latency_probe._call_names(ev) == []

    def test_empty_event(self):
        assert latency_probe._call_names({}) == []


class TestFmt:
    def test_renders_buckets_and_tools(self):
        result = {
            "prompt": "hi",
            "total_s": 12.3,
            "buckets": {"startup": 8.0, "mcp_tool": 1.0, "llm": 3.3},
            "n_domain_tools": 2,
            "tool_calls": ["search_flights", "book_flight"],
        }
        out = latency_probe._fmt(result)
        assert "total" in out
        assert "startup" in out and "mcp" in out and "llm" in out
        assert "2 tools" in out
        assert "search_flights" in out
