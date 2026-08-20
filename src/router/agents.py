"""Multi-model router — one direct-tools agent that swaps model AND prompt per tier.

A complexity classifier (``before_agent_callback``) scores each prompt and picks
a tier (lite → flash → pro → sonnet → opus). Per request the router then adopts
that tier's specialization on TWO axes: a ``before_model_callback`` writes the
tier's concrete model id onto the request so :class:`TierRoutingLlm` (the agent's
single ``model``) runs the turn on that model, and an ``InstructionProvider``
(:func:`tier_instruction_provider`) serves that tier's (GEPA-optimized)
instruction. So a lite lookup and an opus planning task get both the right model
and the right prompt — the full behavior of the old five sub-agents.

**Why not sub_agents / transfer_to_agent (the previous design):** on the deployed
Agent Engine runtime, only the *root* agent's own output streams back. Delegation
via ``transfer_to_agent`` never streamed the transferred specialist's turn
(measured ~0/8 full completions), the same wall the coordinator hit with nested
``AgentTool`` MCP calls. So the router now holds the MCP toolsets DIRECTLY on the
root and varies model+prompt per tier instead of delegating — the proven-streaming
pattern. See ``docs/notes/router-transfer-streaming.md``.
"""

import contextlib

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.genai.types import Content

from src.agents.caching_preload_memory_tool import CachingPreloadMemoryTool
from src.agents.flash_agent import INSTRUCTION as FLASH_INSTRUCTION
from src.agents.lite_agent import INSTRUCTION as LITE_INSTRUCTION
from src.agents.opus_agent import INSTRUCTION as OPUS_INSTRUCTION
from src.agents.pro_agent import INSTRUCTION as PRO_INSTRUCTION
from src.agents.sonnet_agent import INSTRUCTION as SONNET_INSTRUCTION
from src.armor.config import input_guardrail_callback
from src.config import (
    BOOKING_MCP_SERVER,
    COMPLEXITY_HIGH,
    COMPLEXITY_LOW,
    ENABLE_MEMORY_PRELOAD_CACHE,
    EXPENSE_MCP_SERVER,
    FLASH_MODEL,
    HIGH_SPLIT,
    LITE_MODEL,
    MEDIUM_SPLIT,
    OPUS_MODEL,
    PRO_MODEL,
    SEARCH_MCP_SERVER,
    SONNET_MODEL,
    resolve_model,
)
from src.observability.tracing import set_span_attributes, traced
from src.registry import get_mcp_tools

from .complexity import classify_complexity, score_to_model_tier, tier_to_model
from .tier_routing_llm import TierRoutingLlm

# Tier models the dispatcher can run a turn on, in ascending cost/capability
# order. The first is the default (used before the classifier picks a tier and as
# the fallback for an unknown request model).
TIER_MODELS = [LITE_MODEL, FLASH_MODEL, PRO_MODEL, SONNET_MODEL, OPUS_MODEL]


def _mcp_tools():
    return [
        get_mcp_tools(SEARCH_MCP_SERVER),
        get_mcp_tools(BOOKING_MCP_SERVER),
        get_mcp_tools(EXPENSE_MCP_SERVER),
    ]


def _memory_tool():
    """The PreloadMemoryTool variant to use.

    Stock ``PreloadMemoryTool`` issues a blocking Memory Bank retrieve before
    EVERY internal LLM hop; a multi-hop router turn (tool call → synthesis) pays
    that 3-5s retrieve repeatedly, stacking latency ahead of the first streamed
    event and pushing borderline turns into an empty-at-200 timeout. Opt-in
    ``ENABLE_MEMORY_PRELOAD_CACHE`` swaps in :class:`CachingPreloadMemoryTool`,
    which memoizes the retrieve per ``(invocation_id, query)`` — one retrieve per
    turn, zero cross-invocation staleness. Same knob the coordinator uses
    (``src/agents/coordinator_agent.py``); default off ⇒ stock behavior.
    """
    return CachingPreloadMemoryTool() if ENABLE_MEMORY_PRELOAD_CACHE else PreloadMemoryTool()


def _sub_agent_tools():
    return [*_mcp_tools(), _memory_tool()]


# Standalone per-tier agent definitions. These are NO LONGER the router's
# delegation targets (the router is a single direct-tools agent — see below); they
# are retained for standalone per-tier deploy/eval and as the GEPA optimization
# sandbox roots (src/router/<tier>_agent_opt/ import these).
#
# **Built LAZILY (PEP 562 module __getattr__).** Each one calls
# ``_sub_agent_tools()`` => 3 McpToolsets apiece, so eagerly building all five
# made the *serving* container construct 7 agents and perform 18 Agent Registry
# toolset lookups at import for agents the router never invokes (it holds its own
# toolsets directly and reuses only the tier INSTRUCTION constants). Measured
# effect: 7 agents → 2, 18 lookups → 3, import ~8.3s → ~7.3s. Resident memory was
# NOT the win here — RSS measured 265.5MB before vs 265.3MB after, i.e. unchanged;
# the router's real +140MB over the coordinator was ``litellm``, fixed separately
# by resolving tier backbones lazily in :mod:`src.router.tier_routing_llm`.
# Importing ``lite_agent`` & co. by name still works exactly as before; nothing is
# built until first access.
_TIER_AGENT_SPECS: dict[str, tuple[str, str, str]] = {
    "lite_agent": (
        LITE_MODEL,
        "Handles trivial, single-intent lookups — direct facts, single policy checks.",
        LITE_INSTRUCTION,
    ),
    "flash_agent": (
        FLASH_MODEL,
        "Handles simple tasks with light reasoning — formatted searches, single submissions.",
        FLASH_INSTRUCTION,
    ),
    "pro_agent": (
        PRO_MODEL,
        "Handles moderate tasks requiring reasoning — comparisons, multi-step lookups, policy analysis.",
        PRO_INSTRUCTION,
    ),
    "sonnet_agent": (
        SONNET_MODEL,
        "Handles complex, multi-intent requests requiring cross-domain analysis.",
        SONNET_INSTRUCTION,
    ),
    "opus_agent": (
        OPUS_MODEL,
        "Handles expert-level requests requiring deep multi-step planning, budget optimization, and strategic synthesis.",
        OPUS_INSTRUCTION,
    ),
}

# Cache for lazily-built tier agents. Deliberately NOT written back into the
# module namespace: ``__getattr__`` only fires while the name is absent from
# globals(), and keeping it out keeps "was anything built?" observable.
_tier_agents: dict[str, LlmAgent] = {}


def _build_tier_agent(name: str) -> LlmAgent:
    model_id, description, instruction = _TIER_AGENT_SPECS[name]
    return LlmAgent(
        model=resolve_model(model_id),
        name=name,
        description=description,
        instruction=instruction,
        tools=_sub_agent_tools(),
    )


def __getattr__(name: str) -> LlmAgent:
    """PEP 562 hook: build a standalone tier agent on first access.

    Keeps ``from src.router.agents import opus_agent`` working for the GEPA
    sandboxes and the standalone deploy/eval paths without paying their
    construction cost on the router's serving path.
    """
    if name in _TIER_AGENT_SPECS:
        if name not in _tier_agents:
            _tier_agents[name] = _build_tier_agent(name)
        return _tier_agents[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_TIER_AGENT_SPECS])


async def complexity_router_callback(callback_context=None, **kwargs):
    """Classify prompt complexity and store the chosen tier in state.

    The tier is read back by :func:`select_tier_model_callback` (before each LLM
    hop) to select the model the turn actually runs on. Also runs the client-side
    input guardrail and records a per-request routing span.
    """
    if callback_context is None:
        return None
    user_message = ""
    if callback_context and callback_context.user_content:
        if isinstance(callback_context.user_content, Content):
            for part in callback_context.user_content.parts or []:
                if part.text:
                    user_message += part.text
        elif isinstance(callback_context.user_content, str):
            user_message = callback_context.user_content

    if not user_message:
        return None

    guardrail_result = input_guardrail_callback(callback_context=callback_context)
    if guardrail_result is not None:
        return guardrail_result

    # The money shot: a per-request span that records WHY this query routed
    # where it did — score, chosen tier, resolved model, and the boundaries the
    # decision was measured against. Transparent no-op when telemetry is off.
    with traced("router.route"):
        result = await classify_complexity(user_message)
        model_tier = score_to_model_tier(result.score)
        model_id = tier_to_model(model_tier)
        set_span_attributes(
            **{
                "complexity.score": result.score,
                "complexity.level": result.level,
                "routing.tier": model_tier,
                "model.id": model_id,
                "boundaries.low": COMPLEXITY_LOW,
                "boundaries.medium_split": MEDIUM_SPLIT,
                "boundaries.high": COMPLEXITY_HIGH,
                "boundaries.high_split": HIGH_SPLIT,
            }
        )
        callback_context.state["complexity_level"] = result.level
        callback_context.state["complexity_score"] = result.score
        callback_context.state["complexity_reason"] = result.reason
        callback_context.state["model_tier"] = model_tier
    return None


def select_tier_model_callback(callback_context=None, llm_request=None, **kwargs):
    """before_model_callback: point this turn's request at the chosen tier model.

    :class:`TierRoutingLlm` (the agent's ``model``) reads ``llm_request.model`` and
    forwards to that tier's pre-resolved backbone. Runs before every LLM hop so a
    multi-hop turn stays on the tier the classifier picked. No tier in state (e.g.
    an empty user message) leaves the request on the dispatcher's default.
    """
    if callback_context is None or llm_request is None:
        return None
    state = getattr(callback_context, "state", None)
    tier = state.get("model_tier") if state else None
    if tier:
        llm_request.model = tier_to_model(tier)
    return None


async def save_memories_callback(callback_context: CallbackContext | None = None, **kwargs):
    """Persist session events to Memory Bank after each turn."""
    if callback_context is None:
        return None
    with contextlib.suppress(Exception):
        await callback_context.add_session_to_memory()
    return None


# Generic fallback instruction, used only when no tier has been chosen yet (e.g. an
# empty user message that short-circuits complexity_router_callback before it sets
# a tier). The normal path uses the per-tier instruction below.
ROUTER_INSTRUCTION = """\
You are a corporate travel and expense assistant. Fulfill the user's request
DIRECTLY using your tools — never say you are transferring or handing off.

Available tools:
- Flights: use `search_flights` to find flights and `book_flight` to book a
  specific one (e.g. "Book flight FL001"). Ask for missing IDs/details first.
- Hotels: use `search_hotels` to find hotels and `book_hotel` to book one.
- Expenses: ALWAYS use `check_expense_policy` for policy questions and BEFORE any
  submission; use `submit_expense` to submit (submit even if it exceeds policy,
  and tell the user it was flagged for manager review); use `get_user_expenses`
  to show past expenses.

Use recalled memories to personalize responses. Greet the user, ask for
clarification when intent is unclear, and if a request is outside travel/expense,
say so briefly. Keep responses clear and concise, summarizing tool outputs."""


# Per-tier instructions — the SAME (GEPA-optimized) prompts the five standalone
# tier agents carry. The router keeps tier specialization: the classifier picks a
# tier, TierRoutingLlm runs the turn on that tier's MODEL, and this provider serves
# that tier's INSTRUCTION. So a lite lookup and an opus planning task get both the
# right model AND the right prompt — the full routing behavior of the old five
# sub-agents, but on one agent whose output streams end-to-end.
_TIER_TO_INSTRUCTION = {
    "lite": LITE_INSTRUCTION,
    "flash": FLASH_INSTRUCTION,
    "pro": PRO_INSTRUCTION,
    "sonnet": SONNET_INSTRUCTION,
    "opus": OPUS_INSTRUCTION,
}


def tier_instruction_provider(ctx) -> str:
    """InstructionProvider: serve the chosen tier's instruction per request.

    ADK calls this (with a ``ReadonlyContext``) while building each LLM request,
    after ``complexity_router_callback`` has stored ``state["model_tier"]``. It
    mirrors :func:`select_tier_model_callback` (which selects the model) so the
    prompt and the backbone always match the classifier's tier. Falls back to the
    generic :data:`ROUTER_INSTRUCTION` when no tier is set.
    """
    state = getattr(ctx, "state", None)
    tier = state.get("model_tier") if state else None
    return _TIER_TO_INSTRUCTION.get(tier, ROUTER_INSTRUCTION)


# ONE root agent that holds the MCP toolsets DIRECTLY (so its tool calls stream as
# top-level events the managed runtime forwards) and, per request, runs on the
# tier's MODEL (via the TierRoutingLlm dispatcher) with the tier's INSTRUCTION (via
# tier_instruction_provider). No sub_agents / transfer_to_agent — that delegation
# never streamed the specialist's turn on the deployed runtime.
router_agent = LlmAgent(
    model=TierRoutingLlm(TIER_MODELS, default_model=LITE_MODEL),
    name="router_agent",
    instruction=tier_instruction_provider,
    tools=[*_mcp_tools(), _memory_tool()],
    before_agent_callback=complexity_router_callback,
    before_model_callback=select_tier_model_callback,
    after_agent_callback=save_memories_callback,
)

root_agent = router_agent

import types as _t

agent = _t.SimpleNamespace(root_agent=router_agent)
