# `tool_use_quality_v1` on the router: what "no function_call events" means

*Recorded 2026-08-21. Sibling of
[coordinator-tool-use-quality.md](./coordinator-tool-use-quality.md), which covers
the delegation-blind false-negative on the same metric.*

The reported symptom, evaluating the deployed router:

> `tool_use_quality_v1 requires tool calls in the evaluation trace, but no
> function_call/function_response events were found.`

## What the metric actually reads

Not the response text. It is scored from the `AgentData` our own patched parser
assembles out of the engine's `stream_query` event list —
`src/eval/_sdk_patches.py:_patch_single_turn_parser` builds
`AgentData(turns=[ConversationTurn(events=[AgentEvent(...)])], agents=…)`. The
rubric needs at least one `function_call` / `function_response` part **somewhere in
the run**. One tool-using item is enough to produce a score; at exactly zero the
service cannot compute the metric at all and rejects it.

Worse than the error: the harness then **silently reported five metrics instead of
six**, with no line explaining the missing one.

## It regressed — and here is the proof

`eval_outputs/batch_results_multi_agent_eval_20260812_072222.json` records
`router_agent → tool_use_quality_v1 = 0.542` over 12 cases. It worked on 2026-08-12.

On that date the router was the `transfer_to_agent` + `sub_agents` design, and
**`transfer_to_agent` is itself a `function_call`** — so every routed turn emitted a
tool event by construction, whether or not a domain tool ran. The 2026-08-19/20
rearchitecture to one direct-tools agent
([router-transfer-streaming.md](./router-transfer-streaming.md)) deleted that
guarantee. A tool event now appears only if the tier model genuinely calls MCP.

## The router is fine — measured, not assumed

`src/eval/spike_trajectory_visibility.py` against router `6134089059699523584`:

| probe | result |
| --- | --- |
| "Book flight FL001 for Alice Johnson, then find a hotel in New York under $350" | **Branch A** — `booking_mcp_book_flight` + `search_mcp_search_hotels`, both with matching `function_response`s |
| "What's the expense policy for meals?" | `expense_mcp_check_expense_policy` |
| "Check if a $50 transport expense is within policy" | `expense_mcp_check_expense_policy` |
| "Find flights from SFO to JFK" | **no tool call** — answers `"When would you like to travel?"` |

And live batch runs on that engine: 12 cases → `0.42`, 4 cases → `0.44` (3/4 items
called a tool), 3 cases → 2/3. The tool plumbing works.

## The deterministic reproduction

`--limit` slices `cases[:limit]` from the front, and `ROUTER_EVAL_CASES[0]` is the
one prompt that produces no tool call:

```bash
uv run python -m src.eval.multi_agent_batch_eval \
  --agents router_agent --agent-id 6134089059699523584 --limit 1
```

0 of 1 items call a tool ⇒ the metric is unscorable ⇒ before this change, a silent
five-metric run. Any slice of 3+ includes the expense cases and scores normally.

## What changed

1. **The skip is now legible.** `count_tool_call_items()` counts tool-bearing items
   from the `agent_data` column before the eval is created (reusing
   `trajectory_eval.extract_trajectory` / `returned_tool_names`, with
   `include_transfers=True` because the metric counts any function call). Every run
   now prints `Tool calls: N/M items invoked at least one tool` — which also makes a
   *low* score interpretable — and when N is 0 it drops the metric with a named
   reason instead of letting the service reject it.
2. **The router descriptor matches the deployed agent.** `_build_router_info()` still
   declared `sub_agents=["lite_agent", …]` and an instruction that said "delegate".
   Those tier agents are unreachable from the root agent. Now one direct-tools agent.
3. **`AgentConfig.tools` is populated.** The field is real
   (`list[google.genai.types.Tool]`) and *no* descriptor in the repo set it —
   coordinator, router, travel, expense, tiers. The judge was never told what any
   agent could call. `batch_eval.declared_tools()` now builds
   `FunctionDeclaration`s from the real MCP tool ids, descriptions lifted from the
   servers' docstrings.
4. **The engine is resolved per agent.** `run_multi_agent_batch_eval` resolved ONE
   engine for the whole run, defaulting to `AGENT_ENGINE_ID` — a *coordinator*
   (`3639024497392091136` = `coordinator_agent_jt1`). A bare invocation, and
   `run_all_evals`, therefore scored `ROUTER_EVAL_CASES` against a coordinator and
   said nothing. `router_agent` now defaults to `ROUTER_ENGINE_ID`; an explicit
   `--agent-id` still pins every agent (the bake-off needs that).

## Declaring `tools` moved the score — measured, and it should

Same engines, same cases, ~1h apart on 2026-08-21:

| agent | before (no `tools` declared) | after |
| --- | --- | --- |
| router (12 cases) | 0.42, 0.44, 0.48 | **0.29, 0.28** |
| coordinator (49 cases) | 0.36, 0.38, 0.38, 0.39, 0.40, 0.42 | **0.35** |

The coordinator barely moved (0.35 sits just under a 0.36-0.42 spread). The router
dropped ~0.15 consistently across two runs, and the mechanism is the intended one:
the judge can now see that `search_mcp_search_flights` was *available* and was not
called, where before it could only infer an inventory from the trace. Only 5-8 of
the router's 12 items call a tool on any given run — the tool-call rate is itself
nondeterministic — so there is plenty for a now-better-informed judge to penalise.

**Treat this as a level shift in `tool_use_quality_v1` dated 2026-08-21, and do not
compare across it.** What is *not* affected:

- The monitored `custom.googleapis.com/agent_eval/tool_use_accuracy` series.
  `publish_offline_eval._inject_tool_use_accuracy` **overwrites** the batch's
  tool-use score with the standalone `geap_tool_use` judge before publishing on both
  publish paths, and that judge scores `(prompt, final-response-text)` — it never
  reads `AgentConfig.tools`.
- Anything reading the other five rubrics.

What **is** affected: `src/doe/harvest.py` (`BATCH_METRICS`) and
`src/doe/analyze.py` (`QUALITY_METRICS` includes `tool_use_quality`), so DOE
main-effects and bake-off reports spanning this date mix two scales.

Also observed in passing: the coordinator's `hallucination` read 0.42 on this run,
continuing the drift recorded in
[adk-2.7.1-dependency-refresh.md](./adk-2.7.1-dependency-refresh.md) (0.60 / 0.67 /
0.62 post-upgrade vs a 0.73 pre-upgrade mean). Unrelated to this change — nothing
here touches that metric — but it strengthens the case for the judge-drift
investigation that note leaves open.

## Known defect deliberately NOT fixed here

`ROUTER_EVAL_CASES[0]` is **under-specified relative to its own reference**: the
prompt is `"Find flights from SFO to JFK"` but the reference expects
`"United FL001 at $450, Delta FL002 at $520"`. Without a date the router asks for
one — correct behaviour — and the case scores `0.00` on both
`instruction_following` and `final_response_match`. The sibling suites phrase the
same case as `"Find flights from SFO to JFK on June 15"`
(`batch_eval.py:53`, `TRAVEL_EVAL_CASES`).

It is left alone **because `ROUTER_EVAL_CASES` is not only a rubric dataset**: the
same list is fed to `complexity_metrics.run_complexity_accuracy_eval`
(`run_all_evals.py:152`, `pipelines/components.py:122`), whose accuracy is published
as the monitored, alerting `custom.googleapis.com/agent_router/routing_accuracy_pct`
(alert `< 80`). Editing a prompt or adding cases moves that series' inputs. Worth
doing — with a recorded before/after — as its own change, not folded into a bug fix.

## Triage checklist

1. `Tool calls: 0/N` in the run output ⇒ the metric was skipped, and why.
2. Confirm the agent really can call tools:
   `uv run python -m src.eval.spike_trajectory_visibility --agent-id <ID>`.
3. Check the run header — it now prints the engine per agent. Router cases on a
   coordinator engine is a wiring mistake, not an agent regression.
4. Remember `--limit` slices from the front; a small limit can land entirely on
   tool-free prompts.
