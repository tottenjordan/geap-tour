"""Offline tests for the invocation-scoped memory-preload cache.

``CachingPreloadMemoryTool`` subclasses ADK's ``PreloadMemoryTool`` and collapses
the per-LLM-hop ``search_memory`` network round-trip: within a single invocation
(all internal hops share one ``invocation_id`` and one original user query) the
retrieve happens once; a *new* invocation always misses (zero cross-invocation
staleness, so a fact added between requests is never masked — the property the
cross-session-recall demo depends on).

No live GCP: a fake tool context records ``search_memory`` calls and returns
canned :class:`SearchMemoryResponse` objects.
"""

from google.adk.memory.base_memory_service import SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types

from src.agents.caching_preload_memory_tool import CachingPreloadMemoryTool


def _memory(text: str, *, author: str = "user", timestamp: str = "2026-01-01T00:00:00Z"):
    return MemoryEntry(
        content=types.Content(parts=[types.Part(text=text)]),
        author=author,
        timestamp=timestamp,
    )


def _response(*texts: str) -> SearchMemoryResponse:
    return SearchMemoryResponse(memories=[_memory(t) for t in texts])


class _FakeToolContext:
    """Minimal stand-in for ADK's ToolContext used by process_llm_request."""

    def __init__(self, query, invocation_id, response, *, raises=False):
        self.invocation_id = invocation_id
        self._response = response
        self._raises = raises
        self.calls = 0
        self.queries: list[str] = []
        if query is None:
            self.user_content = None
        else:
            self.user_content = types.Content(parts=[types.Part(text=query)])

    async def search_memory(self, query):
        self.calls += 1
        self.queries.append(query)
        if self._raises:
            raise RuntimeError("memory service down")
        return self._response


class _FakeLlmRequest:
    def __init__(self):
        self.appended: list[str] = []

    def _append_dynamic_instructions(self, instructions):
        self.appended.extend(instructions)


class TestCachingWithinInvocation:
    async def test_same_invocation_and_query_fetches_once(self):
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext("what are my prefs?", "inv-1", _response("likes window seat"))
        req1, req2 = _FakeLlmRequest(), _FakeLlmRequest()

        await tool.process_llm_request(tool_context=ctx, llm_request=req1)
        await tool.process_llm_request(tool_context=ctx, llm_request=req2)

        assert ctx.calls == 1  # network retrieve collapsed across hops
        assert req1.appended and req2.appended  # both hops still get the instruction
        assert "likes window seat" in req1.appended[0]
        assert "likes window seat" in req2.appended[0]

    async def test_new_invocation_refetches(self):
        # Zero cross-invocation staleness: a new invocation_id always misses.
        tool = CachingPreloadMemoryTool()
        resp = _response("likes window seat")
        ctx_a = _FakeToolContext("what are my prefs?", "inv-1", resp)
        ctx_b = _FakeToolContext("what are my prefs?", "inv-2", resp)

        await tool.process_llm_request(tool_context=ctx_a, llm_request=_FakeLlmRequest())
        await tool.process_llm_request(tool_context=ctx_b, llm_request=_FakeLlmRequest())

        assert ctx_a.calls == 1
        assert ctx_b.calls == 1  # not served from ctx_a's cache

    async def test_different_query_same_invocation_refetches(self):
        tool = CachingPreloadMemoryTool()
        ctx1 = _FakeToolContext("prefs?", "inv-1", _response("a"))
        ctx2 = _FakeToolContext("bookings?", "inv-1", _response("b"))

        await tool.process_llm_request(tool_context=ctx1, llm_request=_FakeLlmRequest())
        await tool.process_llm_request(tool_context=ctx2, llm_request=_FakeLlmRequest())

        assert ctx1.calls == 1
        assert ctx2.calls == 1  # different query key → separate retrieve


class TestRendering:
    async def test_renders_past_conversations_block(self):
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext("q", "inv-1", _response("prefers Delta", "stays at Marriott"))
        req = _FakeLlmRequest()

        await tool.process_llm_request(tool_context=ctx, llm_request=req)

        si = req.appended[0]
        assert "<PAST_CONVERSATIONS>" in si
        assert "prefers Delta" in si
        assert "stays at Marriott" in si


class TestNoOpPaths:
    async def test_empty_user_content_does_not_fetch(self):
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext(None, "inv-1", _response("x"))
        req = _FakeLlmRequest()

        await tool.process_llm_request(tool_context=ctx, llm_request=req)

        assert ctx.calls == 0
        assert req.appended == []

    async def test_empty_memories_cached_and_not_appended(self):
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext("q", "inv-1", _response())  # no memories
        req1, req2 = _FakeLlmRequest(), _FakeLlmRequest()

        await tool.process_llm_request(tool_context=ctx, llm_request=req1)
        await tool.process_llm_request(tool_context=ctx, llm_request=req2)

        assert ctx.calls == 1  # empty result still cached → second hop no refetch
        assert req1.appended == [] and req2.appended == []

    async def test_search_failure_not_cached(self):
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext("q", "inv-1", None, raises=True)
        req1, req2 = _FakeLlmRequest(), _FakeLlmRequest()

        await tool.process_llm_request(tool_context=ctx, llm_request=req1)
        await tool.process_llm_request(tool_context=ctx, llm_request=req2)

        assert ctx.calls == 2  # transient failure is retried, not cached
        assert req1.appended == [] and req2.appended == []


class TestPreloadSpan:
    """The preload is the coordinator's biggest un-traced latency contributor.

    ``docs/notes/coordinator-latency-attribution.md`` measures the Memory Bank
    retrieve at 3-5s per invocation, and until now it emitted no span at all —
    a trace could not tell you whether a slow turn was the model or the memory
    fetch, nor whether the cache actually collapsed the per-hop retrieves.
    """

    def _preload_spans(self, exporter):
        return [s for s in exporter.get_finished_spans() if s.name == "coordinator.memory_preload"]

    async def test_span_records_cache_hit_and_result_count(self, span_exporter):
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext("prefs?", "inv-1", _response("likes window seat"))

        await tool.process_llm_request(tool_context=ctx, llm_request=_FakeLlmRequest())
        await tool.process_llm_request(tool_context=ctx, llm_request=_FakeLlmRequest())

        spans = self._preload_spans(span_exporter)
        assert len(spans) == 2  # one span per hop, even when the retrieve is cached
        assert [s.attributes["memory.cache_hit"] for s in spans] == [False, True]
        assert spans[0].attributes["memory.result_count"] == 1
        assert spans[0].attributes["memory.invocation_id"] == "inv-1"

    async def test_span_marks_a_failed_retrieve(self, span_exporter):
        """A preload failure was a logger.warning and nothing else."""
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext("q", "inv-1", None, raises=True)

        await tool.process_llm_request(tool_context=ctx, llm_request=_FakeLlmRequest())

        span = self._preload_spans(span_exporter)[0]
        assert span.attributes["memory.error"] == "RuntimeError"
        assert span.attributes["memory.cache_hit"] is False

    async def test_no_span_when_there_is_nothing_to_preload(self, span_exporter):
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext(None, "inv-1", _response("x"))

        await tool.process_llm_request(tool_context=ctx, llm_request=_FakeLlmRequest())

        assert self._preload_spans(span_exporter) == []


class TestEviction:
    async def test_cache_is_bounded(self):
        tool = CachingPreloadMemoryTool(maxsize=2)
        # Three distinct invocations → oldest evicted, cache never exceeds maxsize.
        for i in range(3):
            ctx = _FakeToolContext("q", f"inv-{i}", _response("m"))
            await tool.process_llm_request(tool_context=ctx, llm_request=_FakeLlmRequest())
        assert tool.cache_size <= 2

        # inv-0 was evicted → re-querying it misses (refetches), proving eviction.
        ctx0 = _FakeToolContext("q", "inv-0", _response("m"))
        await tool.process_llm_request(tool_context=ctx0, llm_request=_FakeLlmRequest())
        assert ctx0.calls == 1
