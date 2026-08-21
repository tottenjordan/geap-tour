"""Coordinator Agent — handles travel and expense requests with direct MCP tools.

Integrates Vertex AI Agent Engine Memory Bank so the agent remembers user
interactions (past bookings, expense submissions, preferences) across sessions.
"""

import contextlib

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from src.agents.caching_preload_memory_tool import CachingPreloadMemoryTool
from src.armor.config import (
    get_armored_generate_config,
    guardrail_with_telemetry,
    server_side_armor_enabled,
)
from src.config import (
    BOOKING_MCP_SERVER,
    COORDINATOR_MODEL,
    ENABLE_MEMORY_BANK,
    ENABLE_MEMORY_PRELOAD_CACHE,
    EXPENSE_MCP_SERVER,
    SEARCH_MCP_SERVER,
)
from src.models.quota_retry import retrying_model
from src.observability.tracing import set_span_attributes, traced
from src.registry import get_mcp_tools

# GEPA-optimized (opt-20260807-v6, candidate 2: valset 0.88 -> 1.0). Ported from
# the optimizer sandbox (src/agents/coordinator/agent.py).
#
# The delegation lines were hand-adapted glue (not GEPA output) and were REMOVED
# BY HAND on 2026-08-20: a Cloud Trace census over 10 coordinator invocations
# recorded ZERO AgentTool calls, and a nested sub-agent MCP call does not stream
# back through the deployed Agent Engine runtime anyway. The coordinator holds
# the search/booking/expense MCP toolsets DIRECTLY and books/checks/submits
# itself (Section 1). Section 2's complex-expense capability was folded into the
# "User Expense Retrieval" bullet rather than dropped. The edits were surgical
# deletions; every other sentence is still verbatim optimizer output. Before/after
# rubric A/B in docs/notes/coordinator-router-learnings.md.
#
# HAND-EDITED AGAIN 2026-08-21 (owner decision), two surgical changes:
#   * The opening line still said the agent's role was to "efficiently route user
#     requests" — a behaviour it cannot perform since the AgentTools were removed,
#     and the first sentence the model reads. Now states the direct-tools topology.
#   * Added the "Booking Management" bullet. The coordinator holds the WHOLE booking
#     toolset, so cancel_booking / get_booking_details / list_all_bookings were
#     callable but described by no prompt and covered by no eval case — the gap that
#     let the declared tool inventory sit at 7 of 10 unnoticed.
# Everything else remains verbatim optimizer output. See
# docs/notes/prompt-architecture-audit.md.
INSTRUCTION = """\
You are a corporate assistant coordinator. Your primary role is to fulfil user requests yourself, directly, using your own tools. You have no sub-agents and never hand a request off.

1.  **Direct Tool Usage (Your Primary Action):**
    *   **Flight Search:** Use `search_flights` directly for find/search requests. If invalid airport codes are returned by the tool, inform the user clearly.
    *   **Hotel Search:** Use `search_hotels` directly for hotel find/search requests.
    *   **Flight/Hotel Booking:** Use `book_flight` / `book_hotel` directly to book a specific flight or hotel (e.g. "Book flight FL001"). If the flight/hotel ID or other required booking details are missing, ask the user for them, then book directly and provide the confirmation.
    *   **Booking Management:** Use `get_booking_details` to look up an existing booking by its id, `cancel_booking` to cancel one, and `list_all_bookings` to show recent bookings. Report the returned status honestly, including when a result set is truncated.
    *   **Expense Policy Checks:** **Always** use `check_expense_policy` directly for policy questions *and before submitting any expense*. Although general expense limits are listed here for your quick reference (meals: $75, transport: $200, lodging: $400, supplies: $100, entertainment: $150), you must invoke the `check_expense_policy` tool to get the definitive policy details and ensure accuracy, even if you believe you know the answer.
    *   **Expense Submission:** Use `submit_expense` directly for requests to submit expenses.
        *   **Policy Violation Handling:** When an expense submission is requested, first use `check_expense_policy`. Regardless of whether the expense is within policy or exceeds it, proceed to submit the expense using `submit_expense`. When responding to the user, clearly state if the expense is within policy or if it exceeds the policy limit and has been submitted for review (e.g., "The $X expense exceeds the Y limit. It has been submitted for manager review."). Do not refuse to submit an expense if it exceeds policy; instead, flag it for review.
    *   **User Expense Retrieval:** Use `get_user_expenses` directly to show past expenses, including questions about expense status, appeals, and detailed expense reporting.

2.  **Memory Bank for Personalization:**
    *   Use recalled memories to personalize responses — greet returning users by referencing their recent bookings, preferred airlines, or past expense submissions.

3.  **Greeting and Clarification:**
    *   Always greet the user warmly.
    *   If the user's intent is unclear (e.g., "I need a flight" without details), ask for more details to determine if it's a search or a booking request. Once the intent is clear, proceed with direct tool usage.
    *   If a request is outside your defined capabilities (e.g., weather forecast), politely state that you cannot assist with that specific request and briefly mention the types of tasks you *can* help with (e.g., "I focus on travel booking and expense management"). Avoid proactively offering services unless they are directly related and a logical next step to a *successfully fulfilled* request.

4.  **Proactive Assistance:**
    *   After providing information from a direct tool usage (e.g., hotel search results or expense policy confirmation), proactively suggest the next logical step, such as offering to book a listed option or another relevant next step.

When a request comes in, first determine if you can fulfill it directly using your tools. Always provide the most direct, efficient, and helpful assistance. Your responses should be clear and **concise**, summarizing key information from tool outputs effectively, and guiding the user to their next step. Focus on providing the most relevant details without excessive verbosity."""


async def save_memories_callback(callback_context: CallbackContext):
    """after_agent_callback: persist this session's events to Memory Bank.

    Also annotates the active request span with session/user correlation
    attributes plus the coordinator's config inputs (backbone, memory flags,
    whether server-side armor is live), so a trace answers "what served this,
    and with what wiring?" the way the router's ``router.route`` span does. The
    coordinator's before_agent_callback slot is now the governance guardrail
    (``guardrail_with_telemetry``); this after-callback keeps only the
    memory-persist + correlation-attribute duties.

    Per-tool latency spans are emitted automatically by ADK's own
    instrumentation when telemetry is enabled, so we don't wrap the MCP tool
    calls by hand here — this callback only adds the correlation attributes.
    """
    session = getattr(callback_context, "session", None)
    set_span_attributes(
        **{
            "session.id": getattr(session, "id", None),
            "user.id": getattr(callback_context, "user_id", None),
            # The coordinator's decision inputs, mirroring what the router
            # publishes on its `router.route` span. `model.id` matters most: the
            # backbone moves with COORDINATOR_MODEL (bake-off, DOE points, the
            # 2.5 pin) and a trace otherwise can't say which one served a turn.
            "model.id": COORDINATOR_MODEL,
            "memory.enabled": ENABLE_MEMORY_BANK,
            "memory.cache_enabled": ENABLE_MEMORY_PRELOAD_CACHE,
            "armor.server_side": server_side_armor_enabled(COORDINATOR_MODEL),
        }
    )
    # `suppress` is deliberately the OUTER manager: `traced` records the
    # exception and sets ERROR status on the span before `suppress` swallows it,
    # so a Memory Bank write failure stops being completely invisible without
    # changing the turn's behavior.
    with contextlib.suppress(Exception), traced("coordinator.memory_save"):
        await callback_context.add_session_to_memory()
    return None


# Memory Bank is a DOE factor (`memory_bank`): when ENABLE_MEMORY_BANK is off the
# coordinator drops the PreloadMemoryTool (no recall) and the memory-save
# after-callback (no write), which also makes deploy._wants_memory() False so the
# engine is wrapped session-only with no Memory Bank service. Default is on.
def _build_memory_tools(*, enable_bank: bool, enable_cache: bool) -> list:
    """Select the Memory Bank preload tool for the coordinator's tool list.

    - bank off → no memory tool (and no recall).
    - bank on, cache off → stock ``PreloadMemoryTool`` (current default behavior).
    - bank on, cache on → ``CachingPreloadMemoryTool`` (opt-in latency knob:
      collapses the per-hop retrieve; a subclass, so ``deploy._wants_memory()``
      still detects it and provisions the Memory Bank service).
    """
    if not enable_bank:
        return []
    return [CachingPreloadMemoryTool() if enable_cache else PreloadMemoryTool()]


_memory_tools = _build_memory_tools(
    enable_bank=ENABLE_MEMORY_BANK, enable_cache=ENABLE_MEMORY_PRELOAD_CACHE
)
_after_callback = save_memories_callback if ENABLE_MEMORY_BANK else None

coordinator_agent = LlmAgent(
    # ``resolve_model`` wrapped so a Vertex 429 (RESOURCE_EXHAUSTED) is retried
    # with backoff instead of surfacing as an empty-at-200 stream — google-genai
    # raises, ADK yields nothing, and the caller gets HTTP 200 with zero
    # characters. The router took 215 of those in two hours; the coordinator has
    # taken none so far, so this is insurance, not a fix for an observed defect.
    # See docs/notes/router-empty-responses-quota.md.
    model=retrying_model(COORDINATOR_MODEL),
    name="coordinator_agent",
    instruction=INSTRUCTION,
    tools=[
        # Direct MCP toolsets — the GEPA instruction's Section 1 drives the
        # coordinator to call search_flights/search_hotels, book_flight/
        # book_hotel, AND check_expense_policy/submit_expense/get_user_expenses
        # DIRECTLY. The booking + expense toolsets are held here (not only inside
        # travel_agent/expense_agent) because AgentTool delegation to a sub-agent
        # that then makes a nested MCP call does not stream back through the
        # deployed Agent Engine runtime (works in-process locally, stalls on the
        # managed runtime). Keeping these direct turns them into the
        # coordinator's own tool calls, which the deployed runtime streams
        # correctly.
        get_mcp_tools(SEARCH_MCP_SERVER),
        get_mcp_tools(BOOKING_MCP_SERVER),
        get_mcp_tools(EXPENSE_MCP_SERVER),
        *_memory_tools,
        # No AgentTools: travel_agent/expense_agent were never called (0 across a
        # 10-invocation trace census) and delegation lands on the non-streaming
        # path above. They remain as independently deployed + evaluated agents
        # (multi_agent_batch_eval --agents travel_agent, simulated_eval, their own
        # evalsets) — two deployables, not duplication.
    ],
    # Server-side Model Armor: templates screen prompt + response for injection,
    # unsafe content, sensitive data, and malicious URLs. The region-scoped
    # templates are honored only on the Gemini 2.x regional path, so armor is
    # attached only for a Gemini-2.x backbone; on the global Gemini-3 endpoint the
    # templates 400 (TEMPLATE_NOT_FOUND) and Claude runs via LiteLlm, so armor is
    # omitted there. The client-side callback below is the guaranteed layer for all
    # backbones. See docs/notes/gemini3-native-model-resolution.md.
    generate_content_config=get_armored_generate_config(COORDINATOR_MODEL),
    # Client-side governance guardrail (must-have): rejects prompt-injection /
    # oversized inputs BEFORE the model runs, and emits a span event + metric on
    # each block so the BLOCK is observable. Telemetry never affects the decision.
    before_agent_callback=guardrail_with_telemetry,
    after_agent_callback=_after_callback,
)

root_agent = coordinator_agent

import types as _t

agent = _t.SimpleNamespace(root_agent=coordinator_agent)
