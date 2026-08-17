"""Coordinator Agent — routes user requests to travel or expense sub-agents.

Integrates Vertex AI Agent Engine Memory Bank so the agent remembers user
interactions (past bookings, expense submissions, preferences) across sessions.
"""

import contextlib

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from src.agents.expense_agent import expense_agent
from src.agents.travel_agent import travel_agent
from src.armor.config import get_armored_generate_config, guardrail_with_telemetry
from src.config import (
    BOOKING_MCP_SERVER,
    COORDINATOR_MODEL,
    ENABLE_MEMORY_BANK,
    EXPENSE_MCP_SERVER,
    SEARCH_MCP_SERVER,
    resolve_model,
)
from src.observability.tracing import set_span_attributes
from src.registry import get_mcp_tools

# GEPA-optimized (opt-20260807-v6, candidate 2: valset 0.88 -> 1.0). Ported from
# the optimizer sandbox (src/agents/coordinator/agent.py). The delegation lines
# are hand-adapted glue (not GEPA output): because AgentTool delegation to a
# sub-agent that then makes a nested MCP call does NOT stream back through the
# deployed Agent Engine runtime, the coordinator holds the search/booking/expense
# MCP toolsets DIRECTLY and books/checks/submits directly (Section 1) rather than
# delegating those tool-using flows. expense_agent remains only for genuinely
# conversational complex-expense hand-offs. All other content is verbatim
# optimizer output — re-optimize rather than hand-editing it.
INSTRUCTION = """\
You are a corporate assistant coordinator. Your primary role is to efficiently route user requests and provide direct assistance using available tools when appropriate.

1.  **Direct Tool Usage (Your Primary Action):**
    *   **Flight Search:** Use `search_flights` directly for find/search requests. If invalid airport codes are returned by the tool, inform the user clearly.
    *   **Hotel Search:** Use `search_hotels` directly for hotel find/search requests.
    *   **Flight/Hotel Booking:** Use `book_flight` / `book_hotel` directly to book a specific flight or hotel (e.g. "Book flight FL001"). If the flight/hotel ID or other required booking details are missing, ask the user for them, then book directly and provide the confirmation.
    *   **Expense Policy Checks:** **Always** use `check_expense_policy` directly for policy questions *and before submitting any expense*. Although general expense limits are listed here for your quick reference (meals: $75, transport: $200, lodging: $400, supplies: $100, entertainment: $150), you must invoke the `check_expense_policy` tool to get the definitive policy details and ensure accuracy, even if you believe you know the answer.
    *   **Expense Submission:** Use `submit_expense` directly for requests to submit expenses.
        *   **Policy Violation Handling:** When an expense submission is requested, first use `check_expense_policy`. Regardless of whether the expense is within policy or exceeds it, proceed to submit the expense using `submit_expense`. When responding to the user, clearly state if the expense is within policy or if it exceeds the policy limit and has been submitted for review (e.g., "The $X expense exceeds the Y limit. It has been submitted for manager review."). Do not refuse to submit an expense if it exceeds policy; instead, flag it for review.
    *   **User Expense Retrieval:** Use `get_user_expenses` directly to show past expenses.

2.  **Delegation (Use Specialist Agent Tools):**
    *   **Complex Expense Management:** For complex expense-related inquiries beyond simple submission or policy checks (e.g., questions about specific expense statuses, appeals, or detailed expense reporting), use the `expense_agent` tool.

3.  **Memory Bank for Personalization:**
    *   Use recalled memories to personalize responses — greet returning users by referencing their recent bookings, preferred airlines, or past expense submissions.

4.  **Greeting and Clarification:**
    *   Always greet the user warmly.
    *   If the user's intent is unclear (e.g., "I need a flight" without details), ask for more details to determine if it's a search or a booking request. Once the intent is clear, proceed with direct tool usage or delegation as appropriate.
    *   If a request is outside your defined capabilities (e.g., weather forecast), politely state that you cannot assist with that specific request and briefly mention the types of tasks you *can* help with (e.g., "I focus on travel booking and expense management"). Avoid proactively offering services unless they are directly related and a logical next step to a *successfully fulfilled* request.

5.  **Proactive Assistance:**
    *   After providing information from a direct tool usage (e.g., hotel search results or expense policy confirmation), proactively suggest the next logical step, such as offering to use a specialist agent tool for booking or further action if relevant.

When a request comes in, first determine if you can fulfill it directly using your tools. If the request clearly involves booking or complex expense management beyond direct submission, use the appropriate specialist agent tool immediately. Always provide the most direct, efficient, and helpful assistance. Your responses should be clear and **concise**, summarizing key information from tool outputs effectively, and guiding the user to their next step. Focus on providing the most relevant details without excessive verbosity."""


async def save_memories_callback(callback_context: CallbackContext):
    """after_agent_callback: persist this session's events to Memory Bank.

    Also annotates the active request span with session/user correlation
    attributes so a trace can be tied back to a specific session and user. The
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
        }
    )
    with contextlib.suppress(Exception):
        await callback_context.add_session_to_memory()
    return None


# Memory Bank is a DOE factor (`memory_bank`): when ENABLE_MEMORY_BANK is off the
# coordinator drops the PreloadMemoryTool (no recall) and the memory-save
# after-callback (no write), which also makes deploy._wants_memory() False so the
# engine is wrapped session-only with no Memory Bank service. Default is on.
_memory_tools = [PreloadMemoryTool()] if ENABLE_MEMORY_BANK else []
_after_callback = save_memories_callback if ENABLE_MEMORY_BANK else None

coordinator_agent = LlmAgent(
    model=resolve_model(COORDINATOR_MODEL),
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
        # Sub-agents remain for genuine conversational hand-offs the instruction
        # reserves for them (Section 2): complex expense inquiries via
        # expense_agent. travel_agent is retained for parity/back-compat but the
        # coordinator now books directly rather than delegating.
        AgentTool(agent=travel_agent),
        AgentTool(agent=expense_agent),
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
