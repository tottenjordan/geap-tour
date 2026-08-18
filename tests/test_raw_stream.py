"""Offline tests for the raw-SSE Agent Engine stream client (no live GCP).

The HTTP ``post`` and the auth ``token`` are injected, so the full
session → stream → parse path is exercised without network or credentials.
"""

from src.eval.raw_stream import (
    _endpoint_base,
    capture_pairs,
    capture_triples,
    create_session,
    parse_sse_line,
    stream_query_events,
)


class _FakeResponse:
    """Minimal stand-in for a ``requests`` response."""

    def __init__(self, *, json_body=None, lines=None, status_code=200):
        self._json = json_body
        self._lines = lines or []
        self.status_code = status_code

    def json(self):
        return self._json

    def iter_lines(self, decode_unicode=True):
        yield from self._lines


def _fake_post_factory(*, session_id="S1", stream_lines=None):
    """Return a ``post`` that answers :query (session) and :streamQuery (stream)."""
    calls = []

    def fake_post(url, *, headers, json_body, stream):
        calls.append({"url": url, "headers": headers, "json_body": json_body, "stream": stream})
        if url.endswith(":query"):
            return _FakeResponse(json_body={"output": {"id": session_id}})
        return _FakeResponse(lines=stream_lines or [])

    fake_post.calls = calls
    return fake_post


# --------------------------------------------------------------------------- #
# parse_sse_line
# --------------------------------------------------------------------------- #
class TestParseSseLine:
    def test_plain_json_object(self):
        assert parse_sse_line('{"content": {"parts": []}}') == {"content": {"parts": []}}

    def test_strips_data_prefix(self):
        assert parse_sse_line('data: {"a": 1}') == {"a": 1}

    def test_blank_and_none_are_skipped(self):
        assert parse_sse_line("") is None
        assert parse_sse_line("   ") is None
        assert parse_sse_line(None) is None

    def test_done_sentinel_skipped(self):
        assert parse_sse_line("[DONE]") is None
        assert parse_sse_line("data: [DONE]") is None

    def test_non_json_skipped(self):
        assert parse_sse_line("not json {") is None

    def test_json_array_is_not_an_event(self):
        # We only want top-level objects; a bare array line is not an event.
        assert parse_sse_line("[1, 2, 3]") is None


# --------------------------------------------------------------------------- #
# endpoint / session / stream
# --------------------------------------------------------------------------- #
def test_endpoint_base_uses_resource_region():
    name = "projects/p/locations/us-central1/reasoningEngines/123"
    assert _endpoint_base(name) == "https://us-central1-aiplatform.googleapis.com/v1"


def test_endpoint_base_honors_non_default_region():
    name = "projects/p/locations/europe-west4/reasoningEngines/123"
    assert _endpoint_base(name) == "https://europe-west4-aiplatform.googleapis.com/v1"


def test_create_session_returns_id_and_calls_query():
    post = _fake_post_factory(session_id="SESS42")
    name = "projects/p/locations/us-central1/reasoningEngines/123"
    sid = create_session(name, "alice", token="tok", post=post)
    assert sid == "SESS42"
    call = post.calls[0]
    assert call["url"].endswith(f"/{name}:query")
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["json_body"]["class_method"] == "create_session"
    assert call["json_body"]["input"]["user_id"] == "alice"


def test_stream_query_events_parses_object_per_line():
    lines = [
        '{"content": {"parts": [{"text": "hi"}]}}',
        '{"content": {"parts": [{"text": " there"}]}}',
    ]
    post = _fake_post_factory(stream_lines=lines)
    name = "projects/p/locations/us-central1/reasoningEngines/123"
    events = stream_query_events(
        name, message="q", user_id="u", session_id="S1", token="t", post=post
    )
    assert [e["content"]["parts"][0]["text"] for e in events] == ["hi", " there"]
    stream_call = post.calls[-1]
    assert stream_call["url"].endswith(":streamQuery?alt=sse")
    assert stream_call["stream"] is True
    assert stream_call["json_body"]["input"]["session_id"] == "S1"


# --------------------------------------------------------------------------- #
# capture helpers (drop-in for capture_live_interactions / faithfulness)
# --------------------------------------------------------------------------- #
def test_capture_pairs_joins_visible_text():
    lines = [
        '{"content": {"parts": [{"text": "Flight "}]}}',
        '{"content": {"parts": [{"text": "booked."}]}}',
    ]
    post = _fake_post_factory(stream_lines=lines)
    name = "projects/p/locations/us-central1/reasoningEngines/123"
    pairs = capture_pairs(name, ["book it"], user_id="alice", token="t", post=post)
    assert pairs == [("book it", "Flight booked.")]


def test_capture_triples_retains_trajectory():
    lines = [
        '{"content": {"parts": [{"function_call": {"name": "book_flight", "args": {"flight_id": "FL001"}}}]}}',
        '{"content": {"parts": [{"function_response": {"name": "book_flight", "response": {}}}]}}',
        '{"content": {"parts": [{"text": "Done."}]}}',
    ]
    post = _fake_post_factory(stream_lines=lines)
    name = "projects/p/locations/us-central1/reasoningEngines/123"
    triples = capture_triples(name, ["book FL001"], user_id="alice", token="t", post=post)
    assert len(triples) == 1
    t = triples[0]
    assert t["prompt"] == "book FL001"
    assert t["response"] == "Done."
    assert t["actual_trajectory"] == [
        {"tool_name": "book_flight", "tool_input": {"flight_id": "FL001"}, "returned": True}
    ]


def test_capture_triples_drops_transfer_by_default():
    lines = [
        '{"content": {"parts": [{"function_call": {"name": "transfer_to_agent", "args": {}}}]}}',
        '{"content": {"parts": [{"function_call": {"name": "book_flight", "args": {}}}]}}',
    ]
    post = _fake_post_factory(stream_lines=lines)
    name = "projects/p/locations/us-central1/reasoningEngines/123"
    triples = capture_triples(name, ["x"], token="t", post=post)
    names = [c["tool_name"] for c in triples[0]["actual_trajectory"]]
    assert names == ["book_flight"]
