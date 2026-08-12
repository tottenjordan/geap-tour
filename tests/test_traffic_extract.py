"""_extract_text must pull assistant text out of the real ADK event schema.

Events arrive as dicts shaped ``{"content": {"parts": [{"text": ...}]}}`` — the
old inline extraction only checked a top-level ``text`` key, so it always logged
an empty response even when the agent answered. Thought parts and tool calls
must be skipped so only the visible answer is captured.
"""

from src.traffic.generate_traffic import _extract_text


def test_extract_nested_content_parts():
    ev = {"content": {"parts": [{"text": "Hello, "}, {"text": "world."}]}}
    assert _extract_text(ev) == "Hello, world."


def test_extract_skips_thought_and_tool_parts():
    ev = {
        "content": {
            "parts": [
                {"text": "thinking...", "thought": True},
                {"function_call": {"name": "search"}},
                {"text": "Here are your flights."},
            ]
        }
    }
    assert _extract_text(ev) == "Here are your flights."


def test_extract_top_level_text_fallback():
    assert _extract_text({"text": "legacy shape"}) == "legacy shape"


def test_extract_object_with_text_attr():
    class Chunk:
        text = "obj shape"

    assert _extract_text(Chunk()) == "obj shape"


def test_extract_returns_empty_for_no_text():
    assert _extract_text({"content": {"parts": [{"function_call": {}}]}}) == ""
    assert _extract_text({}) == ""
