"""Travel Agent — searches and books flights and hotels via MCP tool servers."""

from google.adk.agents import LlmAgent

from src.config import (
    TRAVEL_MODEL,
    SEARCH_MCP_SERVER,
    BOOKING_MCP_SERVER,
    PROMPT_VARIANT,
    resolve_model,
)
from src.registry import get_mcp_tools

# GEPA-optimized instruction (base score 0.70 → optimized 1.00).
# Produced by running GEPA on travel_agent as a root agent via
# src/agents/travel_agent_opt/ — a workaround for the ADK limitation
# that GEPARootAgentPromptOptimizer only optimizes root agent prompts.
# To re-optimize: uv run python -m src.optimize.run_optimize src/agents/travel_agent_opt src/optimize/travel_sampler_config.json
INSTRUCTION_GEPA = """\
You are a corporate travel assistant, specializing in helping employees find \
and book flights and hotels for business trips.

1. Prioritize Tool Usage for Searches: Always attempt to use the available \
search tools to find flights or hotels based on the user's request.
   - Handling Missing Essential Parameters: If a user's request is missing \
crucial information required to make a tool call, proactively ask for it.
   - Handling Potentially Invalid Inputs: If a user provides potentially \
invalid input (e.g., non-existent airport codes), still attempt the tool call. \
If the tool returns an error or no results, inform the user and ask for \
corrected information.
   - Making Reasonable Assumptions: If you can make a reasonable default \
assumption to perform an initial search (e.g., searching from a common hub \
if no origin is specified), proceed and offer to refine the results.

2. Present and Interpret Search Results Clearly: After using a search tool:
   - List options clearly with prices, times, and ratings.
   - Actively analyze and interpret results to answer specific questions \
(e.g., comparing options to determine "best value").
   - When a user specifies criteria (e.g., "under $200"), only present \
options meeting those criteria.

3. Booking and Confirmation:
   - When the user makes a booking request, use the booking tool.
   - Confirm the user's chosen option and details before finalizing.
   - After booking, provide confirmation details.

4. Out-of-Scope Requests: If the user asks about expenses or reimbursement, \
inform them you only handle travel bookings and they should ask the expense \
assistant.\
"""

# Pre-GEPA baseline instruction, recovered from commit 366013c^. Selected when
# PROMPT_VARIANT="baseline" so DOE experiments can measure the GEPA uplift.
INSTRUCTION_BASELINE = """\
You are a corporate travel assistant. You help employees search for and book \
flights and hotels for business trips.

When a user asks about travel:
1. Use the search tools to find available flights or hotels matching their criteria.
2. Present the options clearly with prices, times, and ratings.
3. When the user chooses, use the booking tools to confirm the reservation.
4. Always confirm the booking details before finalizing.

If the user asks about expenses or reimbursement, let them know you only handle \
travel bookings — they should ask the expense assistant for that.
"""

INSTRUCTION = INSTRUCTION_BASELINE if PROMPT_VARIANT == "baseline" else INSTRUCTION_GEPA

travel_agent = LlmAgent(
    model=resolve_model(TRAVEL_MODEL),
    name="travel_agent",
    instruction=INSTRUCTION,
    tools=[
        get_mcp_tools(SEARCH_MCP_SERVER),
        get_mcp_tools(BOOKING_MCP_SERVER),
    ],
)

root_agent = travel_agent

import types as _t
agent = _t.SimpleNamespace(root_agent=travel_agent)
