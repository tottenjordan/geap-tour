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
from google.adk.models.llm_request import LlmRequest
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
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


def _request() -> LlmRequest:
    """A **real** ``LlmRequest``, deliberately not a duck-typed fake.

    The previous fake implemented ``_append_dynamic_instructions`` itself, so it
    kept passing when ADK 2.7.0 moved the preload render to
    ``_insert_transient_user_content`` — a fake that mirrors the API it is meant
    to be checking can never catch upstream drift. Both methods still exist on
    ``LlmRequest`` in 2.7.1, which is exactly what made the divergence silent.
    """
    return LlmRequest(
        model="gemini-2.5-flash",
        contents=[types.Content(role="user", parts=[types.Part(text="hi")])],
    )


def _preloaded(req: LlmRequest) -> list[str]:
    """Memory blocks the tool injected, read from *both* channels ADK has used.

    Old (ADK <= 2.6.x): the dynamic-instruction list. New (>= 2.7.0): transient
    user content. Reading both keeps this helper version-agnostic;
    ``test_render_matches_stock_adk`` is what pins which channel is correct for
    the installed ADK.
    """
    texts = list(getattr(req, "_dynamic_instructions", None) or [])
    texts += [
        part.text
        for content in req.contents
        for part in (content.parts or [])
        if part.text and "<PAST_CONVERSATIONS>" in part.text
    ]
    return texts


class TestCachingWithinInvocation:
    async def test_same_invocation_and_query_fetches_once(self):
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext("what are my prefs?", "inv-1", _response("likes window seat"))
        req1, req2 = _request(), _request()

        await tool.process_llm_request(tool_context=ctx, llm_request=req1)
        await tool.process_llm_request(tool_context=ctx, llm_request=req2)

        assert ctx.calls == 1  # network retrieve collapsed across hops
        assert _preloaded(req1) and _preloaded(req2)  # both hops still get the instruction
        assert "likes window seat" in _preloaded(req1)[0]
        assert "likes window seat" in _preloaded(req2)[0]

    async def test_new_invocation_refetches(self):
        # Zero cross-invocation staleness: a new invocation_id always misses.
        tool = CachingPreloadMemoryTool()
        resp = _response("likes window seat")
        ctx_a = _FakeToolContext("what are my prefs?", "inv-1", resp)
        ctx_b = _FakeToolContext("what are my prefs?", "inv-2", resp)

        await tool.process_llm_request(tool_context=ctx_a, llm_request=_request())
        await tool.process_llm_request(tool_context=ctx_b, llm_request=_request())

        assert ctx_a.calls == 1
        assert ctx_b.calls == 1  # not served from ctx_a's cache

    async def test_different_query_same_invocation_refetches(self):
        tool = CachingPreloadMemoryTool()
        ctx1 = _FakeToolContext("prefs?", "inv-1", _response("a"))
        ctx2 = _FakeToolContext("bookings?", "inv-1", _response("b"))

        await tool.process_llm_request(tool_context=ctx1, llm_request=_request())
        await tool.process_llm_request(tool_context=ctx2, llm_request=_request())

        assert ctx1.calls == 1
        assert ctx2.calls == 1  # different query key → separate retrieve


class TestRendering:
    async def test_render_matches_stock_adk(self):
        """Differential guard: our render must be byte-identical to ADK's own.

        ``CachingPreloadMemoryTool`` only means to memoize the *retrieve*; the
        render is a verbatim copy of the parent's, which ADK inlines into
        ``process_llm_request`` with no hook to delegate to. So the copy can rot
        silently — and it did: ADK 2.7.0 moved the memory block off
        ``_append_dynamic_instructions`` (system-instruction channel) onto
        ``_insert_transient_user_content`` (a user turn placed at the
        current-turn boundary). Both methods still exist in 2.7.1, so the stale
        copy kept "working" while putting the memories somewhere else in the
        prompt entirely.

        Comparing whole requests rather than asserting a specific channel means
        the next upstream move fails here instead of quietly degrading recall.
        """
        resp = _response("prefers Delta", "stays at Marriott")
        stock_req, cached_req = _request(), _request()

        await PreloadMemoryTool().process_llm_request(
            tool_context=_FakeToolContext("q", "inv-1", resp), llm_request=stock_req
        )
        await CachingPreloadMemoryTool().process_llm_request(
            tool_context=_FakeToolContext("q", "inv-1", resp), llm_request=cached_req
        )

        assert cached_req.model_dump() == stock_req.model_dump()
        # model_dump() skips pydantic private attrs, so check that channel too.
        assert cached_req._dynamic_instructions == stock_req._dynamic_instructions
        assert _preloaded(cached_req) == _preloaded(stock_req) != []

    async def test_renders_past_conversations_block(self):
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext("q", "inv-1", _response("prefers Delta", "stays at Marriott"))
        req = _request()

        await tool.process_llm_request(tool_context=ctx, llm_request=req)

        si = _preloaded(req)[0]
        assert "<PAST_CONVERSATIONS>" in si
        assert "prefers Delta" in si
        assert "stays at Marriott" in si


class TestNoOpPaths:
    async def test_empty_user_content_does_not_fetch(self):
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext(None, "inv-1", _response("x"))
        req = _request()

        await tool.process_llm_request(tool_context=ctx, llm_request=req)

        assert ctx.calls == 0
        assert _preloaded(req) == []

    async def test_empty_memories_cached_and_not_appended(self):
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext("q", "inv-1", _response())  # no memories
        req1, req2 = _request(), _request()

        await tool.process_llm_request(tool_context=ctx, llm_request=req1)
        await tool.process_llm_request(tool_context=ctx, llm_request=req2)

        assert ctx.calls == 1  # empty result still cached → second hop no refetch
        assert _preloaded(req1) == [] and _preloaded(req2) == []

    async def test_search_failure_not_cached(self):
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext("q", "inv-1", None, raises=True)
        req1, req2 = _request(), _request()

        await tool.process_llm_request(tool_context=ctx, llm_request=req1)
        await tool.process_llm_request(tool_context=ctx, llm_request=req2)

        assert ctx.calls == 2  # transient failure is retried, not cached
        assert _preloaded(req1) == [] and _preloaded(req2) == []


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

        await tool.process_llm_request(tool_context=ctx, llm_request=_request())
        await tool.process_llm_request(tool_context=ctx, llm_request=_request())

        spans = self._preload_spans(span_exporter)
        assert len(spans) == 2  # one span per hop, even when the retrieve is cached
        assert [s.attributes["memory.cache_hit"] for s in spans] == [False, True]
        assert spans[0].attributes["memory.result_count"] == 1
        assert spans[0].attributes["memory.invocation_id"] == "inv-1"

    async def test_span_marks_a_failed_retrieve(self, span_exporter):
        """A preload failure was a logger.warning and nothing else."""
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext("q", "inv-1", None, raises=True)

        await tool.process_llm_request(tool_context=ctx, llm_request=_request())

        span = self._preload_spans(span_exporter)[0]
        assert span.attributes["memory.error"] == "RuntimeError"
        assert span.attributes["memory.cache_hit"] is False

    async def test_no_span_when_there_is_nothing_to_preload(self, span_exporter):
        tool = CachingPreloadMemoryTool()
        ctx = _FakeToolContext(None, "inv-1", _response("x"))

        await tool.process_llm_request(tool_context=ctx, llm_request=_request())

        assert self._preload_spans(span_exporter) == []


class TestEviction:
    async def test_cache_is_bounded(self):
        tool = CachingPreloadMemoryTool(maxsize=2)
        # Three distinct invocations → oldest evicted, cache never exceeds maxsize.
        for i in range(3):
            ctx = _FakeToolContext("q", f"inv-{i}", _response("m"))
            await tool.process_llm_request(tool_context=ctx, llm_request=_request())
        assert tool.cache_size <= 2

        # inv-0 was evicted → re-querying it misses (refetches), proving eviction.
        ctx0 = _FakeToolContext("q", "inv-0", _response("m"))
        await tool.process_llm_request(tool_context=ctx0, llm_request=_request())
        assert ctx0.calls == 1
