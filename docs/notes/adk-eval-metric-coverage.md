# ADK eval metrics: what we use, what we don't, and why

*Audited 2026-08-21, against ADK 2.7.1's `PrebuiltMetrics` registry.*

## First finding: the 2.6.3 → 2.7.1 upgrade added **no** eval metrics

Diffed the whole `google/adk/evaluation/` tree between the two wheels. **No modules
added or removed**, and `PrebuiltMetrics` is byte-identical — 13 metrics in both. The
eval changes in 2.7.1 are hardening: a shared `_get_metric_threshold` that rejects a
metric with no threshold instead of passing `None` through, `description` made
optional, and `custom_function_path` moved to a private attr so an inbound payload
can't smuggle one.

Two things worth knowing because they *look* new and aren't:

- **`ToolTrajectoryCriterion.MatchType.IN_ORDER` existed in 2.6.3.** We just adopted
  it ([trajectory-criterion.md](./trajectory-criterion.md)); it was available all along.
- **`custom_function_path` existed in 2.6.3** — registering a custom metric function
  in an eval config is not a new capability.

So the useful question isn't "what's new?" but **"what's available and unused?"**

## Coverage before this change: 6 of 13

| metric | where we used it |
| --- | --- |
| `rubric_based_final_response_quality_v1` | `scenarios/*eval_config.json` (`adk eval`) |
| `rubric_based_tool_use_quality_v1` | `scenarios/*eval_config.json` |
| `hallucinations_v1` | `scenarios/*` (with `evaluate_intermediate_nl_responses`) |
| `safety_v1` | scenarios + evalsets + all 9 GEPA sampler configs |
| `multi_turn_task_success_v1` | `scenarios/*` |
| `multi_turn_tool_use_quality_v1` | `scenarios/*` |
| `response_match_score` | `evalsets/*`, GEPA sampler configs |
| `final_response_match_v2` | `evalsets/*`, GEPA sampler configs |

(Separately, the batch eval uses Vertex's own `types.RubricMetric` namespace — a
*different* set from ADK's. The two are easy to confuse; a name valid in one is not
necessarily valid in the other.)

## Adopted

Both added to `scenarios/eval_config.json` and `scenarios/router_eval_config.json` —
the two configs driving the `adk eval` user-simulator runs (`run_user_sim.sh`,
`run_router_eval.sh`). Threshold-only, matching how the other rubric-based metrics are
already configured; ADK auto-generates the rubrics.

### `per_turn_user_simulator_quality_v1`

Grades the **simulated user**, not the agent. Every multi-turn score we produce is
only as trustworthy as the conversation that generated it, and nothing was checking
that the simulator stayed on script. A drifting simulator would have read as an agent
regression — the exact failure mode that cost this project most of a day in
[offline-eval-empty-turns.md](./offline-eval-empty-turns.md) and
[router-tool-use-quality.md](./router-tool-use-quality.md), where infra and harness
problems were showing up as quality scores.

### `rubric_based_multi_turn_trajectory_quality_v1`

Grades the multi-turn **path**. We already scored whether the task succeeded
(`multi_turn_task_success_v1`) and whether tools were used well
(`multi_turn_tool_use_quality_v1`), but never *how the agent got there* across turns.
It accumulates the full dialogue — user turns, agent turns, tool calls and responses —
and scores the last turn with the whole conversation as context. ADK's own docstring
example for it is a travel-booking agent doing multi-step itinerary planning, which is
precisely this domain.

## Deliberately not adopted

**`tool_trajectory_avg_score`** — investigated in depth and deferred; see
[trajectory-criterion.md](./trajectory-criterion.md). Ordering is already 100% correct
on every turn that calls a tool, so it offers no headroom, and the naming/args
questions are unmeasured on the local GEPA path where ADK compares names internally.

**`response_evaluation_score`** — the legacy coherence-style metric. We already score
the final response two ways (`rubric_based_final_response_quality_v1` and
`final_response_match_v2`); a third overlapping signal adds cost and noise, not
information.

**Multi-turn metrics in `evalsets/*eval_config.json`** — that path is single-turn
`adk eval` with no user simulator, so they would score nothing. A guard test asserts
they never leak there.

## Not shipped, but worth doing: rubric pre-generation on the canonical path

`batch_eval.py:788` calls `client.evals.generate_rubrics(...)` before evaluating,
with the comment *"Pre-generate rubrics so the evaluator uses context-aware rubrics
instead of auto-generating ones that may contradict actual behavior."*

**`multi_agent_batch_eval.py` — the path we actually run, and the one that feeds the
monitored `agent_eval/*` series — does not.** The legacy single-agent path has the
better treatment; the canonical one does not.

Given "auto-generated rubrics that contradict actual behaviour" is a near-verbatim
description of what we spent this session fixing, this is probably a real improvement.
It is **not** shipped here because it changes how the monitored series is scored, and
2026-08-21 already carries two level shifts (`tool_use_quality` and `hallucination`,
both from [offline-eval-empty-turns.md](./offline-eval-empty-turns.md)). A third,
bundled into a metrics-coverage change, would make all three uninterpretable. It
deserves its own change with a recorded before/after.

## Guard tests

`tests/test_multi_agent_eval.py::TestAdkEvalMetricCoverage`:

- every metric in every config resolves to a real evaluator via ADK's
  `DEFAULT_METRIC_EVALUATOR_REGISTRY` — a typo or an upstream rename fails here
  rather than deep inside an `adk eval` run;
- every declared name is a real `PrebuiltMetrics` value (an unknown name is otherwise
  silently ignored);
- the simulator paths grade both the simulator and the path;
- the single-turn configs declare no multi-turn or simulator metrics.
