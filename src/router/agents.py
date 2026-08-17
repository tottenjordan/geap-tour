"""Multi-model agent definitions — 5-tier router by prompt complexity.

Routes to: Lite → Flash → Pro → Sonnet → Opus based on classifier score.
"""

import contextlib

import litellm

litellm.suppress_debug_info = True

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.genai.types import Content

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
    EXPENSE_MCP_SERVER,
    FLASH_MODEL,
    HIGH_SPLIT,
    LITE_MODEL,
    MEDIUM_SPLIT,
    OPUS_MODEL,
    PRO_MODEL,
    ROUTER_MODEL,
    SEARCH_MCP_SERVER,
    SONNET_MODEL,
    resolve_model,
)
from src.observability.tracing import set_span_attributes, traced
from src.registry import get_mcp_tools

from .complexity import classify_complexity, score_to_model_tier, tier_to_model


def _mcp_tools():
    return [
        get_mcp_tools(SEARCH_MCP_SERVER),
        get_mcp_tools(BOOKING_MCP_SERVER),
        get_mcp_tools(EXPENSE_MCP_SERVER),
    ]


def _sub_agent_tools():
    return [*_mcp_tools(), PreloadMemoryTool()]


lite_agent = LlmAgent(
    model=resolve_model(LITE_MODEL),
    name="lite_agent",
    description="Handles trivial, single-intent lookups — direct facts, single policy checks.",
    instruction=LITE_INSTRUCTION,
    tools=_sub_agent_tools(),
)

flash_agent = LlmAgent(
    model=resolve_model(FLASH_MODEL),
    name="flash_agent",
    description="Handles simple tasks with light reasoning — formatted searches, single submissions.",
    instruction=FLASH_INSTRUCTION,
    tools=_sub_agent_tools(),
)

pro_agent = LlmAgent(
    model=resolve_model(PRO_MODEL),
    name="pro_agent",
    description="Handles moderate tasks requiring reasoning — comparisons, multi-step lookups, policy analysis.",
    instruction=PRO_INSTRUCTION,
    tools=_sub_agent_tools(),
)

sonnet_agent = LlmAgent(
    model=resolve_model(SONNET_MODEL),
    name="sonnet_agent",
    description="Handles complex, multi-intent requests requiring cross-domain analysis.",
    instruction=SONNET_INSTRUCTION,
    tools=_sub_agent_tools(),
)

opus_agent = LlmAgent(
    model=resolve_model(OPUS_MODEL),
    name="opus_agent",
    description="Handles expert-level requests requiring deep multi-step planning, budget optimization, and strategic synthesis.",
    instruction=OPUS_INSTRUCTION,
    tools=_sub_agent_tools(),
)


async def complexity_router_callback(callback_context=None, **kwargs):
    """Classify prompt complexity and store in state for the router's delegation logic."""
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
    # decision was measured against. Transparent no-op when telemetry is off;
    # routing behavior below is unchanged.
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


async def save_memories_callback(callback_context: CallbackContext | None = None, **kwargs):
    """Persist session events to Memory Bank after each turn."""
    with contextlib.suppress(Exception):
        await callback_context.add_session_to_memory()
    return None


ROUTER_INSTRUCTION = """\
You are a routing coordinator. You MUST always hand the request to a specialist
agent — never answer the user yourself.

A complexity classifier has assessed the user's request:
- Level: {complexity_level}
- Score: {complexity_score}
- Model tier: {model_tier}
- Reason: {complexity_reason}

Transfer control to the specialist agent that matches the model_tier by calling
transfer_to_agent with the agent_name:
- "lite" → transfer_to_agent(agent_name="lite_agent")
- "flash" → transfer_to_agent(agent_name="flash_agent")
- "pro" → transfer_to_agent(agent_name="pro_agent")
- "sonnet" → transfer_to_agent(agent_name="sonnet_agent")
- "opus" → transfer_to_agent(agent_name="opus_agent")

Your ONLY action is that transfer. Do not produce any other text.\
"""

# Delegation is ADK agent transfer (sub_agents), NOT AgentTool tools. AgentTool
# runs a sub-agent as a nested tool call whose nested MCP output must bubble back
# up through the parent — and that nested stream does NOT come back through the
# deployed Agent Engine runtime (works in-process, stalls on the managed
# runtime; see the same note in coordinator_agent.py). transfer_to_agent instead
# makes the chosen specialist the active agent, so its own MCP events stream out
# as top-level events the runtime forwards correctly.
router_agent = LlmAgent(
    model=resolve_model(ROUTER_MODEL),
    name="router_agent",
    instruction=ROUTER_INSTRUCTION,
    tools=[PreloadMemoryTool()],
    sub_agents=[lite_agent, flash_agent, pro_agent, sonnet_agent, opus_agent],
    before_agent_callback=complexity_router_callback,
    after_agent_callback=save_memories_callback,
)

root_agent = router_agent

import types as _t

agent = _t.SimpleNamespace(root_agent=router_agent)
