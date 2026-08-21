"""Invocation-scoped caching wrapper around ADK's ``PreloadMemoryTool``.

ADK runs ``PreloadMemoryTool.process_llm_request`` **before every LLM hop** and,
each time, issues ``tool_context.search_memory(user_query)`` — a blocking network
retrieve against Vertex Memory Bank. But ``user_query`` is
``user_content.parts[0].text``, the *original* message that started the
invocation, which is **constant across all internal hops**. So a multi-hop
coordinator request (initial call → after a tool response → after the next tool →
final answer) re-issues the *identical* retrieve every hop. Measured at ~3-5s per
invocation for a seeded user; a multi-tool booking chain pays it repeatedly
(docs/notes/coordinator-latency-attribution.md).

``CachingPreloadMemoryTool`` memoizes the retrieve keyed by
``(invocation_id, query)``. Within one invocation all hops share the same key, so
the network call happens **once**. A *new* invocation has a fresh ``invocation_id``
(``ReadonlyContext.invocation_id``), so it always misses → **zero cross-invocation
staleness by construction**: a fact added to Memory Bank between two requests can
never be masked by a stale cache entry. That property is what keeps the validated
cross-session-recall demo (``verify_cross_session_recall``) safe.

Opt-in: the coordinator wires this only when ``ENABLE_MEMORY_PRELOAD_CACHE`` is
set (see ``src/config.py`` / ``src/agents/coordinator_agent.py``); default off ⇒
stock ``PreloadMemoryTool`` behavior, unchanged.

Notes / scope:
- Only *successful* retrieves are cached (including an empty-memories result, which
  is a valid answer). A transient ``search_memory`` exception is **not** cached, so
  a later hop retries — matching the parent's per-hop give-up-then-retry behavior.
- The render step (turning memories into the ``<PAST_CONVERSATIONS>`` block) is a
  verbatim copy of ADK's ``PreloadMemoryTool``, which inlines it into
  ``process_llm_request`` with no hook to delegate to. It runs fresh every hop off
  the cached response; only the network retrieve is memoized. Because it is a copy
  it can rot silently — ADK 2.7.0 moved the block from the system-instruction
  channel to a transient user turn while leaving *both* methods on ``LlmRequest`` —
  so ``test_render_matches_stock_adk`` diffs a whole ``LlmRequest`` against the
  stock tool. Keep that test passing when bumping ADK.
- No lock: under asyncio two concurrent invocations that miss the same key could
  double-fetch. That is harmless (same result) and rare, so the single-threaded
  simplicity is preferred over per-key locking.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import TYPE_CHECKING, override

from google.adk.tools import _memory_entry_utils
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.genai import types

from src.observability.tracing import set_span_attributes, traced

if TYPE_CHECKING:
    from google.adk.memory.base_memory_service import SearchMemoryResponse
    from google.adk.models import LlmRequest
    from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger("google_adk." + __name__)

# Per-container cache bound. Keys are (invocation_id, query); each finished
# invocation leaves at most a handful of entries, so a few hundred keeps the
# working set of recent/in-flight invocations while bounding memory.
_DEFAULT_MAXSIZE = 512


class CachingPreloadMemoryTool(PreloadMemoryTool):
    """``PreloadMemoryTool`` that caches the retrieve per ``(invocation_id, query)``."""

    def __init__(self, *, maxsize: int = _DEFAULT_MAXSIZE) -> None:
        super().__init__()
        self._maxsize = max(1, maxsize)
        # insertion-ordered so we can evict the oldest key when over capacity.
        self._cache: OrderedDict[tuple[str, str], SearchMemoryResponse] = OrderedDict()

    @property
    def cache_size(self) -> int:
        """Current number of cached entries (for tests / observability)."""
        return len(self._cache)

    def _get(self, key: tuple[str, str]) -> SearchMemoryResponse | None:
        response = self._cache.get(key)
        if response is not None:
            self._cache.move_to_end(key)  # LRU: mark recently used
        return response

    def _put(self, key: tuple[str, str], response: SearchMemoryResponse) -> None:
        self._cache[key] = response
        self._cache.move_to_end(key)
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)  # evict oldest

    @override
    async def process_llm_request(
        self,
        *,
        tool_context: ToolContext,
        llm_request: LlmRequest,
    ) -> None:
        user_content = tool_context.user_content
        if not user_content or not user_content.parts or not user_content.parts[0].text:
            return

        user_query: str = user_content.parts[0].text
        key = (tool_context.invocation_id, user_query)

        # Traced because this retrieve is the coordinator's dominant un-attributed
        # latency (3-5s per invocation, docs/notes/coordinator-latency-attribution.md)
        # and because ``memory.cache_hit`` is the only way to see from a trace
        # whether the per-hop collapse this class exists for actually happened.
        with traced("coordinator.memory_preload") as span:
            response = self._get(key)
            cache_hit = response is not None
            set_span_attributes(
                **{
                    "memory.cache_hit": cache_hit,
                    "memory.invocation_id": tool_context.invocation_id,
                }
            )
            if not cache_hit:  # cache miss → single network retrieve for this invocation
                try:
                    response = await tool_context.search_memory(user_query)
                except Exception as exc:
                    # Not re-raised (the parent tool also gives up quietly), so
                    # annotate the span by hand — ``traced`` only records
                    # exceptions that propagate out of the block.
                    span.record_exception(exc)
                    set_span_attributes(**{"memory.error": type(exc).__name__})
                    logger.warning("Failed to preload memory for query: %s", user_query)
                    return  # transient failure is NOT cached; a later hop retries
                self._put(key, response)

            set_span_attributes(**{"memory.result_count": len(response.memories or [])})

        self._render(response, llm_request)

    @staticmethod
    def _render(response: SearchMemoryResponse, llm_request: LlmRequest) -> None:
        """Render memories into the dynamic-instruction block (mirrors ADK)."""
        if not response.memories:
            return
        memory_text_lines: list[str] = []
        for memory in response.memories:
            if time_str := (f"Time: {memory.timestamp}" if memory.timestamp else ""):
                memory_text_lines.append(time_str)
            if memory_text := _memory_entry_utils.extract_text(memory):
                memory_text_lines.append(
                    f"{memory.author}: {memory_text}" if memory.author else memory_text
                )
        if not memory_text_lines:
            return
        full_memory_text = "\n".join(memory_text_lines)
        memory_context = f"""The following content is from your previous conversations with the user.
They may be useful for answering the user's current query.
<PAST_CONVERSATIONS>
{full_memory_text}
</PAST_CONVERSATIONS>
"""
        # ADK >= 2.7.0 places the memory block as a transient *user* turn at the
        # current-turn boundary, not in the system-instruction channel it used
        # through 2.6.x (`_append_dynamic_instructions`). Both methods still
        # exist, so calling the old one is a silent downgrade rather than an
        # error — `test_render_matches_stock_adk` diffs a whole LlmRequest
        # against the stock tool so the next upstream move fails loudly.
        llm_request._insert_transient_user_content(
            [types.Content(role="user", parts=[types.Part.from_text(text=memory_context)])]
        )
