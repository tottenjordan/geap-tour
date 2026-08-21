# Coordinator `tool_use` scores ~0.27 — root-cause finding (rubric fix shipped)

> **See also:** [router-tool-use-quality.md](./router-tool-use-quality.md) — the same
> metric on the router, where it errors out entirely instead of scoring low, and the
> explanation of what `tool_use_quality_v1` actually reads (the `AgentData` events).

**Verdict: mis-rubric + measurement-artifact, NOT an agent defect.** The
coordinator's tools work; the metric is measuring the wrong thing. This note
records the investigation and the fix that shipped for cause #1 (the mis-rubric);
cause #2 (trajectory capture) remains open and is called out below.

## Status: fixed for the published series (2026-08-14)

The delegation-aware rubric is now wired into the **offline-eval bridge** via a
standalone judge — Option 2 in the "Recommended follow-up" below. The monitored
`agent_eval/tool_use_accuracy` series now derives from `geap_tool_use`
(`src/eval/tool_use_judge.py`), not the delegation-blind SDK rubric:

- `src/eval/tool_use_judge.py:run_tool_use_eval()` runs the deployed coordinator
  over the tool-expecting cases, calls the judge model directly via
  `google.genai`, and parses the `Score: N` line itself (mirroring
  `src/eval/policy_judge.py`), sidestepping the `client.evals` custom-metric
  `400 Error parsing JSON` bug.
- `src/eval/publish_offline_eval.py:_inject_tool_use_accuracy()` overwrites the
  batch's mis-rubriced tool-use score **in place** with the judge's score;
  `_apply_standalone_judges()` runs it (with `_inject_policy_compliance`) on both
  publish paths — the `publish_offline_eval --run` CLI and `run_all_evals`.

**Scoping — intentionally NOT changed:** `get_metrics()` still wires the generic
`TOOL_USE_QUALITY` in the batch, so DOE `harvest.BATCH_METRICS`, the bake-off,
and fixtures are untouched; only the *published* series is corrected. See the
"Out of scope" reasoning in the plan.

**Still open (cause #2):** the judge scores the (prompt, final-response) pair, not
the raw execution trajectory. If the managed runtime does not surface nested
sub-agent MCP calls into the captured response, the score reflects
request/outcome quality rather than the literal tool trajectory. Treat the number
as honest-but-outcome-based until trajectory capture is verified (see cause #2).

## Symptom

In the coordinator batch eval, `tool_use_quality_v1` is the only metric that
consistently fails the 0.6 threshold, while every other metric passes
comfortably:

| Metric | Score (run 20260813_004220) |
|---|---|
| `final_response_quality_v1` | 0.875 |
| `final_response_match_v2` | 0.862 |
| `instruction_following_v1` | 0.786 |
| `hallucination_v1` | 0.632 |
| `safety_v1` | 0.606 |
| **`tool_use_quality_v1`** | **0.274** ← fails |

It is systematic, not a one-off — across the last 8 batch runs the score sits in
a tight 0.27–0.46 band (0.40, 0.337, 0.394, 0.461, 0.378, 0.310, 0.376, 0.274)
and is the lowest metric every single time.

The intra-run distribution is the tell: for run 20260813_004220,
`tool_use_quality_v1` has `MINIMUM=0`, and `MODE = MEDIAN = MAXIMUM = P90 = P95 =
P99 = 0.3333`. **No item ever scored above one-third**, with variance 0.013. That
is not the spread you get from real quality variation (compare
`final_response_quality`: MODE 1, MINIMUM 0, variance 0.109). It is the signature
of a rubric that structurally cannot award a high score to this agent.

## Root cause

Two compounding causes; both point away from the agent itself.

### 1. Mis-rubric (primary, confirmed)

`src/eval/agent_eval_configs.py:get_metrics()` (line ~545) wires the **generic
predefined** `types.RubricMetric.TOOL_USE_QUALITY`:

```python
return [
    types.RubricMetric.FINAL_RESPONSE_QUALITY,
    ...
    types.RubricMetric.TOOL_USE_QUALITY,   # generic, delegation-blind
    ...
]
```

The coordinator is a **domain router**: its own action on almost every turn is a
single `transfer_to_agent(...)` delegation to `travel_agent` / `expense_agent`,
and the actual MCP tools (`search_flights`, `check_expense_policy`, …) are called
by the sub-agent. A generic tool-use rubric has no reason to treat
`transfer_to_agent` as "correct tool use," so it reads the coordinator as barely
using tools — hence the score pinned at the low tier for every item.

The repo already contains a purpose-built rubric that fixes exactly this:
`TOOL_USE_METRIC` (`name="geap_tool_use"`) in `src/eval/batch_eval.py:244`. Its
instruction is explicit:

> "The delegation pattern (router → sub-agent → tool) is the CORRECT architecture
> — do NOT penalize for using transfer_to_agent."

**It is defined but never wired into `get_metrics()`.** So the eval scores the
coordinator with the delegation-blind rubric while the delegation-aware one sits
unused.

### 2. Measurement-artifact (secondary, suspected — not fully confirmable here)

Even with the right rubric, the score depends on whether the eval trajectory the
judge sees actually contains the sub-agent's MCP tool calls, or only the
coordinator's top-level `transfer_to_agent` step. `run_inference` executes the
real deployed engine (`src/eval/multi_agent_batch_eval.py:107`,
`agent=agent_resource_name`), so the calls do happen — but whether the managed
Agent Engine surfaces the nested sub-agent tool calls into the captured
trajectory is unverified. The locally-saved batch JSON has `item_count: 0` /
`items: []` (the SDK did not persist per-item rationales), so the per-item
"why 1/3" rationale could not be read directly in this investigation. If the
trajectory only carries `transfer_to_agent`, a rubric swap alone will not lift
the score — the trajectory-capture path would need fixing first.

This is the same class of platform trace-content limitation documented for the
native online evaluators (see [[online-eval-content-capture-blocked]] and
[offline-eval-monitoring-bridge](./offline-eval-monitoring-bridge.md)).

## Recommended follow-up

1. **Swap the rubric:** replace `types.RubricMetric.TOOL_USE_QUALITY` in
   `get_metrics()` with the domain-aware `TOOL_USE_METRIC` (`geap_tool_use`).
   *(Not taken — a batch-metric swap ripples into DOE harvest + the bake-off. See
   "Scoping" above: the published series is corrected in the bridge instead.)*
2. **✅ Shipped — standalone judge into the offline bridge.** `geap_tool_use` is a
   custom pointwise `LLMMetric` — the *same* type that forced `policy_compliance`
   off `client.evals` and onto a standalone judge (`400 Error parsing JSON`; see
   the `get_metrics` docstring and `src/eval/policy_judge.py`). The realized fix
   mirrors `policy_compliance`: `src/eval/tool_use_judge.py` runs the deployed
   coordinator, calls the judge model directly via `google.genai`, parses the
   `Score: N` line itself, and feeds the offline-eval bridge
   (`publish_offline_eval._inject_tool_use_accuracy`).
3. **Confirm trajectory capture first.** ⚠️ **Still open.** Verify that the eval
   trajectory for a coordinator item actually includes the sub-agent's MCP tool
   calls. If it does not, the standalone judge scores request/outcome quality, not
   the literal trajectory — an honest improvement over the delegation-blind rubric,
   but not a trajectory-level tool-use measurement. This is cause #2 and remains
   unaddressed.

The `agent_eval/tool_use_accuracy` monitored point now reflects the
delegation-aware judge rather than the ~0.27 delegation-blind rubric, but should
still be read as outcome-based until cause #2 is verified.

Related: [offline-eval-monitoring-bridge](./offline-eval-monitoring-bridge.md),
[[online-eval-content-capture-blocked]] (memory).
