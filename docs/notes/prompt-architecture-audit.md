# Prompt audit: do our prompts describe the system we actually ship?

*Audited 2026-08-21, across every agent instruction, judge rubric, eval descriptor
and the A2A card.*

Prompted by a pattern: the eval descriptors were found stale twice in one week
(`_build_router_info` still declaring five `sub_agents`;
`AgentConfig.tools` populated nowhere). Both were symptoms of the same thing —
the 2026-08-19/20 rearchitecture removed delegation, and the *text* describing the
system did not follow the code.

## Ground truth

- **Coordinator** (`src/agents/coordinator_agent.py`) — one direct-tools agent, no
  `AgentTool`s, no `sub_agents`.
- **Router** (`src/router/agents.py:298`) — one direct-tools agent that swaps model
  and instruction per tier. No `transfer_to_agent`.
- **Tools — 10, not 7.** `verify_mcp_tools.EXPECTED_TOOLS` is the canonical list:
  `search_flights`, `search_hotels`, `book_flight`, `book_hotel`,
  **`cancel_booking`**, **`get_booking_details`**, **`list_all_bookings`**,
  `submit_expense`, `check_expense_policy`, `get_user_expenses`.
- **Policy limits** in prompts (meals $75 / transport $200 / lodging $400 /
  supplies $100 / entertainment $150) match `expense/mock_db.POLICY_LIMITS`
  exactly. No drift. Checked because they are hardcoded in four prompts.

## Fixed

### 1. `geap_tool_use` described a deleted architecture (highest impact)

`batch_eval.TOOL_USE_METRIC` — the rubric whose score **overwrites the monitored
`agent_eval/tool_use_accuracy` series** — opened with:

> *"This is a multi-agent system where a router agent delegates to specialist
> sub-agents via transfer_to_agent … The delegation pattern (router → sub-agent →
> tool) is the CORRECT architecture — do NOT penalize for using
> transfer_to_agent."*

None of that has been true since 2026-08-20. Worse, one of its **four criteria** was
*"Delegation appropriateness: … was it routed to an appropriate specialist? Simple
queries to lite/flash agents…"* — unanswerable for an agent that cannot delegate,
so a quarter of the rubric graded a non-event.

Replaced with the direct-tools premise, the full 10-tool list, and a criterion that
tests something real: **policy-then-submit ordering**, including that an over-limit
expense *must still be submitted* (the server records it `pending_review`).

### 2. The declared tool inventory was 7 of 10

`declared_tools()` (shipped in #66/#68, and load-bearing since #68 feeds it to the
judge) derived its names from the eval cases' `expected_tool`, which only covers the
tools the cases happen to exercise. The three booking-management tools were missing,
so the judge was told the agent lacked tools it holds. Now sourced against
`verify_mcp_tools.EXPECTED_TOOLS`, with a test asserting set equality.

### 3. A2A card omitted expense retrieval

The card advertised five skills but not `get_user_expenses`, despite the
coordinator's instruction having a dedicated "User Expense Retrieval" bullet. Added
`expense_history`. The booking-management tools stay unadvertised **deliberately** —
see the open item below.

## Found, NOT fixed — GEPA prompts are re-optimize-only

`CLAUDE.md`: *"Agent instructions that say 'GEPA-optimized' were produced by the
optimizer — edit these only through re-optimization, not manually."* These are real
defects; hand-patching them would silently de-optimize a measured prompt.

### A. `expense_agent` is instructed to fail its own eval case

`src/agents/expense_agent.py:INSTRUCTION_GEPA`:

> *"submit_expense(...): Only call AFTER check_expense_policy confirms within
> policy. **If over limit, do not submit** — inform the user it requires manager
> review."*

That contradicts three things at once:

| source | says |
| --- | --- |
| `expense/mock_db.submit_expense` | always submits; sets `status="pending_review"` when over limit |
| coordinator instruction | *"Do not refuse to submit an expense if it exceeds policy; instead, flag it for review."* |
| its own eval case | "Submit a $500 entertainment expense…" → reference `Status: pending_review`, `expected_signals` includes `pending_review`, `expected_tool` is `submit_expense` |

So the prompt tells the agent to withhold the tool call its own test requires.
**Fix path:** re-optimize with
`uv run python -m src.optimize.run_optimize src/agents/expense_agent_opt src/optimize/expense_sampler_config.json`,
after correcting the sampler cases so the optimizer targets submit-and-flag.

### B. Coordinator opens with "route user requests"

> *"You are a corporate assistant coordinator. Your primary role is to efficiently
> **route** user requests…"*

Vestigial — everything after it is "Direct Tool Usage (Your Primary Action)". Low
harm, but it is the first line the model reads and it names a behaviour the agent
cannot perform. Roll into the next re-optimization rather than hand-editing; the
file's own comment says every other sentence is verbatim optimizer output.

### C. Three tools no prompt mentions

`cancel_booking`, `get_booking_details` and `list_all_bookings` are held by the
coordinator and the router (both take the whole booking toolset) but are described
by **no** instruction. The model still sees their MCP declarations so it *can* call
them, but nothing steers it to, and no eval case covers them. Either drive them from
a re-optimized prompt with cases, or drop them from the served toolsets — carrying
undescribed, untested tools is the state that let the inventory drift unnoticed.

## Guard tests

`tests/test_multi_agent_eval.py::TestPromptsMatchTheArchitecture` now pins:

- the `geap_tool_use` rubric makes no affirmative delegation claim, and states the
  direct-tools topology (matching on *affirmative* phrases — an earlier draft
  flagged the rubric's own correction, "has no sub-agents");
- the rubric lists every real tool;
- the declared inventory equals the servers' inventory;
- no shipped descriptor declares `sub_agents`.

`tests/test_tool_use_judge.py` previously asserted `"transfer_to_agent" in prompt` —
it was actively **pinning the stale rubric in place**. Inverted.
