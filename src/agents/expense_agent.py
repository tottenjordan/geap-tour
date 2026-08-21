"""Expense Agent — submits expenses and checks corporate policy via MCP tool server."""

from google.adk.agents import LlmAgent

from src.config import EXPENSE_MCP_SERVER, EXPENSE_MODEL, PROMPT_VARIANT, resolve_model
from src.models.afc import with_afc_disabled
from src.registry import get_mcp_tools

# GEPA-optimized instruction (base score 0.60 → optimized 0.90).
# Produced by running GEPA on expense_agent as a root agent via
# src/agents/expense_agent_opt/ — a workaround for the ADK limitation
# that GEPARootAgentPromptOptimizer only optimizes root agent prompts.
# To re-optimize: uv run python -m src.optimize.run_optimize src/agents/expense_agent_opt src/optimize/expense_sampler_config.json
#
# HAND-EDITED 2026-08-21 (owner decision), step 2 only. The optimizer had produced
# "If over limit, do not submit", which contradicts three things: the server
# (submit_expense always records, setting status=pending_review when over limit),
# the coordinator's instruction ("Do not refuse to submit ... flag it for review"),
# and this agent's own eval case, whose reference is "Status: pending_review" with
# expected_tool=submit_expense. The prompt told the agent to withhold the tool call
# its test requires. Surgical: every other sentence is verbatim optimizer output.
# The correct behaviour was present in INSTRUCTION_BASELINE below — GEPA introduced
# this regression, so re-optimization needs corrected sampler cases or it will
# reintroduce it. See docs/notes/prompt-architecture-audit.md.
INSTRUCTION_GEPA = """\
You are a corporate expense management assistant. Help employees manage \
expense reports while adhering to company policies.

Policy limits: meals ($75), transport ($200), lodging ($400), supplies ($100), \
entertainment ($150). Amounts above these limits require manager review.

Tools and process:

1. check_expense_policy(category, amount): Always call this FIRST for any \
policy question or before submitting. If the category is unrecognized, list \
valid categories. If within policy, state the limit. If over, state the limit \
and note it requires manager review.

2. submit_expense(user_id, category, amount, description): Only call AFTER \
check_expense_policy. Submit the expense either way — an over-limit expense is \
still recorded, with status pending_review — then tell the user whether it was \
approved or exceeded the limit and has been flagged for manager review. Never \
refuse to submit because an amount is over policy. Requires user_id — ask for \
it if not provided.

3. get_user_expenses(user_id): Retrieve past expenses for a user.

If the user asks about booking travel, inform them you only handle expenses \
and they should ask the travel assistant.\
"""

# Pre-GEPA baseline instruction, recovered from commit 366013c^. Selected when
# PROMPT_VARIANT="baseline" so DOE experiments can measure the GEPA uplift.
INSTRUCTION_BASELINE = """\
You are a corporate expense management assistant. You help employees submit \
expense reports and check corporate reimbursement policies.

When a user asks about expenses:
1. If they want to check policy, use check_expense_policy first to verify limits.
2. If they want to submit, use submit_expense with all required details.
3. If they want to view past expenses, use get_user_expenses.
4. Always inform the user whether their expense is within policy before submitting.

Policy categories: meals ($75), transport ($200), lodging ($400), supplies ($100), \
entertainment ($150). Amounts above these limits require manager review.

If the user asks about booking travel, let them know you only handle expenses — \
they should ask the travel assistant for that.
"""

INSTRUCTION = INSTRUCTION_BASELINE if PROMPT_VARIANT == "baseline" else INSTRUCTION_GEPA

expense_agent = LlmAgent(
    model=resolve_model(EXPENSE_MODEL),
    name="expense_agent",
    instruction=INSTRUCTION,
    tools=[
        get_mcp_tools(EXPENSE_MCP_SERVER),
    ],
    generate_content_config=with_afc_disabled(),
)

root_agent = expense_agent

import types as _t

agent = _t.SimpleNamespace(root_agent=expense_agent)
