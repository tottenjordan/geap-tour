"""Restore the tool-call ids ADK strips from a *wrapped* LiteLlm backbone.

**Why this exists.** ADK mints synthetic ``adk-<uuid>`` ids for function calls and
then removes them again before replaying history to the model, because Gemini
rejects ids it never issued. It keeps them only for providers that pair a tool
call with its result *by id* — and it decides that with a type check
(``flows/llm_flows/contents.py``)::

    if isinstance(canonical_model, (AnthropicLlm, LiteLlm, OpenAIResponsesLlm)):
        preserve_function_call_ids = True

Our Claude backbones never satisfy it. The router's agent model is a
:class:`~src.router.tier_routing_llm.TierRoutingLlm` dispatcher and each tier sits
inside a :class:`~src.models.quota_retry.RetryingLlm`, so ``canonical_model`` is a
wrapper, not a ``LiteLlm``. ADK therefore strips the ids, and the turn that
replays a tool *result* to Claude dies inside LiteLLM's Anthropic transform::

    convert_to_anthropic_tool_result -> tool_message["tool_call_id"]
    litellm.llms.anthropic.common_utils.AnthropicError: 'tool_call_id'

The exception escapes into ADK, which yields nothing — an **empty-at-200**.

**The precondition is a mixed-tier session.** ADK strips only its *own* prefix
(``AF_FUNCTION_CALL_ID_PREFIX = 'adk-'``), and Claude issues real ``toolu_*`` ids
that LiteLlm records, so a session that only ever ran on Claude is unaffected. The
missing id must have been minted for a provider that issues none — Gemini — on an
earlier turn of the same session. That is the normal case here: the traffic
generator reuses one session per user, and a 5-tier router exists to send
consecutive turns to different tiers. Reproduced end-to-end against real Claude on
Vertex by ``src/eval/spike_tool_call_ids.py`` (no engine required).

Restoring an id is deliberately origin-agnostic: it fills whatever is absent,
whether ADK stripped it or a provider never supplied one.
See docs/notes/router-empty-stream-retry.md.

Re-pairing here rather than unwrapping is deliberate: the wrappers exist for
per-tier dispatch and 429 retries, and ADK's check looks at the *agent's* model,
so even an unwrapped tier would not satisfy it through the dispatcher.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

# Distinct from ADK's ``adk-`` prefix so these ids are attributable in a payload
# dump, and so a future ADK pass that strips ``adk-*`` cannot undo this one.
_ID_PREFIX = "geap-tool"


def is_litellm_backed(model: object) -> bool:
    """True if ``model`` is an ADK ``LiteLlm``, without importing litellm.

    Reads ``sys.modules`` instead of importing: ``src.router.agents`` keeps the
    litellm import lazy because it costs ~140MB resident on a worker that only
    ever serves Gemini tiers. If a ``LiteLlm`` instance exists at all, the module
    is already imported, so this can never miss a real one.
    """
    module = sys.modules.get("google.adk.models.lite_llm")
    lite_llm = getattr(module, "LiteLlm", None) if module is not None else None
    if lite_llm is None:
        return False
    return isinstance(model, lite_llm)


def _parts(llm_request: Any) -> Iterator[Any]:
    for content in getattr(llm_request, "contents", None) or []:
        yield from getattr(content, "parts", None) or []


def restore_tool_call_ids(llm_request: Any) -> int:
    """Give every unidentified function call/response a paired id. Returns the count.

    Calls and responses are paired **by name, in order** — the same rule ADK uses
    when ids are absent — so two hops on the same tool stay distinct instead of
    collapsing onto one id.

    Only *missing* ids are filled, so a provider that supplied real ones keeps
    them and a second pass is a no-op. An orphan response (no matching call, e.g.
    history truncated mid-turn) still gets a synthetic id: a fabricated pairing
    degrades one reply, while a missing key empties the entire stream.

    Parts are shallow copies shared with the session's events, so the nested
    ``FunctionCall``/``FunctionResponse`` is **replaced** rather than mutated —
    writing ``.id`` in place would rewrite recorded history.
    """
    pending: dict[str, list[str]] = {}
    fixed = 0
    counter = 0

    for part in _parts(llm_request):
        call = getattr(part, "function_call", None)
        if call is not None and not getattr(call, "id", None):
            counter += 1
            new_id = f"{_ID_PREFIX}-{counter}"
            part.function_call = call.model_copy(update={"id": new_id})
            pending.setdefault(call.name or "", []).append(new_id)
            fixed += 1
            continue

        response = getattr(part, "function_response", None)
        if response is not None and not getattr(response, "id", None):
            queue = pending.get(response.name or "")
            if queue:
                new_id = queue.pop(0)
            else:
                counter += 1
                new_id = f"{_ID_PREFIX}-orphan-{counter}"
            part.function_response = response.model_copy(update={"id": new_id})
            fixed += 1

    return fixed
