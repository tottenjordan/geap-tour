# GEPA sampler cases: what the optimizer was being taught

*Audited 2026-08-21. Follow-up to
[prompt-architecture-audit.md](./prompt-architecture-audit.md), which found
`expense_agent`'s GEPA prompt instructing the agent to refuse an over-limit
submission and flagged that re-optimizing would reintroduce it.*

GEPA can only learn what the cases show it, so a prompt defect is usually a data
defect. All **13** evalsets were swept — the 9 optimizer training sets
(`src/agents/*_opt/`, `src/agents/coordinator/`, `src/router/*_opt/`,
`src/router/router_eval_set`) and the 4 runtime ADK evalsets
(`src/eval/evalsets/`).

## The case that taught refuse-to-submit

`src/router/flash_agent_opt/flash_eval_set.evalset.json` carried a case whose
`eval_id` was literally **`expense_over_limit_no_submit`**:

| | before |
| --- | --- |
| prompt | "Submit a $500 entertainment expense for team event, user ID EMP002" |
| expected tools | `["check_expense_policy"]` — **no `submit_expense`** |
| reference | *"This expense **cannot be auto-submitted**. It requires manager review. Please get manager approval before resubmitting."* |

`expense/mock_db.submit_expense` **always records** the expense and sets
`status="pending_review"` when it is over limit. There is no "cannot submit" path.
This was the **only** case in the entire 13-file corpus teaching refusal — every
other over-limit submit case correctly submits and flags.

Renamed to `expense_over_limit_submit_and_flag`, given the `submit_expense` call,
and its reference rewritten to state the recorded `pending_review` status.

**Worth noting for the record:** `expense_eval_set` — the set `expense_agent`
actually optimizes against — was already **correct** on this behaviour (reference:
*"It has been submitted but flagged for manager review"*, with `submit_expense` in
the trajectory). So the prompt defect fixed in PR #69 did **not** come from
its own training data; GEPA produced it anyway. The corrected flash case removes the
one place in the corpus that would have reinforced it.

## The systematic defect: submit without a policy check

**11 cases across 6 files** expected `submit_expense` with no preceding
`check_expense_policy`, while every prompt says to check first —
`expense_agent` (*"Always call this FIRST … before submitting"*), the coordinator
(*"**Always** use `check_expense_policy` … before submitting any expense"*) — and
the `geap_tool_use` rubric now scores exactly that ordering.

| file | cases fixed |
| --- | --- |
| `src/agents/coordinator/coordinator_eval_set` | 2 |
| `src/agents/expense_agent_opt/expense_eval_set` | 2 |
| `src/router/router_eval_set` | 2 |
| `src/router/flash_agent_opt/flash_eval_set` | 1 (same case as above) |
| `src/eval/evalsets/coordinator.evalset` | 3 |
| `src/eval/evalsets/expense_agent.evalset` | 2 |
| `src/eval/evalsets/router_agent.evalset` | 2 |

The pattern was already inconsistent *within* single files — `flash_eval_set`'s
`expense_submission_with_policy_check` and `router_eval_set`'s
`high_expense_audit_full` include the check, their neighbours did not — so this was
drift, not a deliberate convention.

## A third find: a spurious expectation

`src/eval/evalsets/coordinator.evalset.json` case `multi_intent_travel_expense`
expected `search_flights(destination="ORD")`, but its prompt is expense-only
("Submit a $30 meal receipt for user EMP001") and its reference mentions only the
expense — a leftover from when the case really was multi-intent. Teaching a model to
search flights on an expense-only request is a hallucinated-action lesson. Removed
the spurious call and renamed the case `expense_submit_low_amount`;
`src/eval/holdout.py` updated to match (the holdout split referenced the old id, and
its test caught the rename).

## What was clean

- **No unknown tools.** Every expected tool exists on a server.
- **No stale `transfer_to_agent` expectations** in any evalset.
- **Policy limits** stated in references all match `POLICY_LIMITS`
  ($75/$200/$400/$100/$150). An initial regex flagged ~20 "wrong limits", all false
  positives — it was matching expense *amounts* adjacent to category words.

## Guard tests

`tests/test_eval_dataset_integrity.py::TestEvalsetCasesMatchTheSystem` sweeps all 13
evalsets (with a test asserting the discovery glob still finds them, so a moved file
can't silently empty the sweep) and pins:

- no `submit_expense` without a preceding `check_expense_policy`;
- no reference teaching refuse-to-submit;
- every expected tool exists on a real server.

## Before re-optimizing

The cases are now consistent with the system, so
`uv run python -m src.optimize.run_optimize src/agents/expense_agent_opt src/optimize/expense_sampler_config.json`
should no longer be able to *learn* the refusal. It is still not guaranteed not to
*invent* it — it did so once from correct data — so re-run the guard tests and diff
the produced instruction against the current one before adopting any new candidate.

One structural caveat: `expense_sampler_config.json`'s criteria are
`response_match_score` (0.04), `final_response_match_v2` (0.3) and `safety_v1` — all
**response-text** metrics. **No trajectory metric is scored**, so the corrected
`tool_uses` do not directly reward calling `check_expense_policy` first; they make
the corpus self-consistent and feed the optimizer's context, but the objective
still only grades the final answer.

> **Followed up 2026-08-21 — see [trajectory-criterion.md](./trajectory-criterion.md).**
> Trajectory is now scored in the **eval** run (`run_all_evals` Phase 4). Adding it to
> the **optimizer** objective is deliberately deferred: the calibration found ordering
> is already 100% correct on every turn that calls a tool, so there is no headroom on
> the incumbent prompt, and the naming/args questions are unmeasured on the *local*
> GEPA path. That note names the experiment that unblocks it.
