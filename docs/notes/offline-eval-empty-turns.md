# The "hallucination drift" was empty turns, not drift

*Investigated 2026-08-21. Supersedes the judge-drift hypothesis left open in
[adk-2.7.1-dependency-refresh.md](./adk-2.7.1-dependency-refresh.md).*

## The claim that started it

The coordinator's `hallucination_v1` looked like it was sliding: a ~0.73 mean before
the ADK 2.7.1 upgrade, 0.60/0.67/0.62 after, then **0.42**. The working hypothesis
was judge-side drift from `google-cloud-aiplatform` 1.163 → 1.165, which ships the
Gen AI Evaluation Service's rubric templates.

**That hypothesis was wrong.** So was a second one — that PR #66's
`AgentConfig.tools` declaration had moved the score. Both are corrected below.

## What it actually is

An eval item whose final response text is **empty** scores ~0 on `hallucination_v1`,
near-deterministically. Per-item pull from two completed runs on the same engine
(`4380288848559603712`), same 49 cases:

| run | items scored | empty final text | mean halluc (empty) | mean halluc (has text) | reported |
| --- | --- | --- | --- | --- | --- |
| 08-21 06:13 | 30 | **11 (37%)** | **0.06** | 0.664 | **0.42** |
| 08-21 02:30 | 47 | **2 (4%)** | 0.00 | 0.824 | **0.81** |

Answer quality barely moved (0.66 vs 0.82 on items that produced text). What moved
was the **share of turns that produced no answer at all**. The reported metric is
largely a proxy for the run's infra-empty rate.

### Why an empty response scores zero rather than being skipped

The judge does not treat an empty response as "no data" — it grades it. With the
response empty, the item's raw `function_call` is rendered into the graded text and
marked contradictory. Verbatim judge rationale from the 0.42 run:

```
sentence: print(default_api.booking_mcp_book_flight(flight_id = "FL999", passenger_name = "Carol Danvers"))
label:    contradictory
rationale: The context explicitly states that the agent has no tools, meaning it
           cannot execute any function calls, including the one presented.
```

Two separate defects in one item: the empty response, **and** the judge being told
the agent has no tools (see below).

### Where the empty comes from

`src/eval/_sdk_patches.py:_extract_final_text` returns `""` when a turn ends on a
`function_call` with no synthesized answer, and `_is_empty_turn` — which drives the
retry — only fired on a *literally empty* event list. A turn with events but no text
was therefore never retried, and flowed straight through to the judges as an empty
string.

This is the same phenomenon the online monitor already handles. Memory
`online-helpfulness-dips-are-empty-streams` and
[online-infra-empty-and-baseline-alerts.md](./online-infra-empty-and-baseline-alerts.md)
record that an online helpfulness dip is usually empty-at-200, and P2.8 partitioned
those out into `agent_online_eval/infra_empty_rate`. **The offline batch never got
the same treatment** — so offline, an infra failure still reads as a quality score.

## What changed

1. **`_is_empty_turn` now also treats "events but no final text" as empty**, so the
   existing retry machinery (`EVAL_EMPTY_RETRIES`, default 4) applies to it. Same
   principle `RetryingLlm` already uses for a silent turn on the serving path. An
   `{"error": …}` dict is still not retried — that is a labelled failure.
2. **Every batch run reports its empty rate**: `Empty responses: N/M (X%) — these
   depress every rubric`, with a warning above 20%, plus `empty_responses` /
   `empty_rate` persisted into the results JSON so past runs are comparable.

## The empty rate predicts the score, ~1:1

Four runs of the same 49 cases against the same engine, now that the rate is
measured:

| run | empty rate | `hallucination_v1` |
| --- | --- | --- |
| 08-21 06:13 (pre-fix) | 37% of scored items | **0.42** |
| 08-21 07:5x (post-fix) | 22% (11/49) | **0.66** |
| 08-21 02:30 (pre-fix) | 4% (2/47) | **0.81** |
| 08-21 08:0x (post-fix) | 4% (2/49) | **0.80** |

Two independent runs at a 4% empty rate both land at 0.80-0.81; runs at 22-37% land
at 0.42-0.66. The metric is close to a linear readout of the empty rate. Nothing
about the model, the prompts, or the judge templates needs to change to move it —
and conversely, no conclusion about answer quality can be drawn from it without the
empty rate beside it.

The retry does not drive the rate to zero (11/49 on one post-fix run): some cases
end on a tool call repeatably, and retrying a deterministic outcome cannot help.
That residue is a genuine agent/runtime behaviour worth its own investigation — the
point of this change is that it is now *visible and attributed* instead of being
silently priced into a hallucination score.

## Two corrections to earlier claims

- **Not judge drift.** No evidence implicates the aiplatform 1.163 → 1.165 rubric
  templates. The 0.73 → 0.63 step recorded in the ADK note sits inside this
  mechanism's noise, and the 0.42 outlier is fully explained by a 37% empty rate.
- **PR #66's `AgentConfig.tools` does not reach the batch eval.** `build_agent_info()`
  is called **only** by `simulated_eval.py`; `multi_agent_batch_eval` and
  `batch_eval` pass a resource-name string to `run_inference` and never an
  `AgentInfo`. Confirmed on the wire: `agent_data.agents` is `None` on every batch
  item. So attributing the router's `tool_use_quality` 0.42 → 0.29 to that change
  was wrong — that movement is run-to-run variance.

## The metric is high-variance — read any single run with that in mind

Coordinator `hallucination_v1` across all recorded runs:

- 20-case runs: **0.37 … 0.97**
- 49-case runs (pre-2026-08-21): 0.61, 0.62, 0.68, 0.69, 0.73, 0.75, 0.81

A two- or three-run comparison cannot resolve a 0.1 shift here. Compare empty rates
before comparing means.

## Open, and worth doing: tell the judge what tools exist

`agent_data.agents` being `None` is a real defect independent of the empties — the
judge is explicitly told the agent has no tools. Passing
`agent_info=build_agent_info(...)` into `create_evaluation_run` populates it, and an
8-case A/B on the coordinator measured:

| metric | without `agent_info` | with |
| --- | --- | --- |
| `tool_use_quality_v1` | 0.417 | **0.944** |
| `hallucination_v1` | 0.914 | 0.889 |

That is a large, well-founded correction to the long-standing ~0.33-0.42 tool-use
score ([coordinator-tool-use-quality.md](./coordinator-tool-use-quality.md)) — the
judge was penalising an agent it had been told had no tools.

**Not shipped here**, because passing `agent_info` also makes the service emit a
*second* candidate series (`coordinator_agent/*` alongside `agent_engine_0/*`) whose
scores are degenerate — 1.0 across the board in one run, 0.45 hallucination in
another. That would corrupt `harvest`/`publish` key matching. Landing it needs the
phantom candidate handled first, and its own before/after, since it moves
`tool_use_quality` by ~0.5.
