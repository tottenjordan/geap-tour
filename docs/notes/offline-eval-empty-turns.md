# The "hallucination drift" was the judge being told the agent has no tools

*Investigated 2026-08-21. Supersedes the judge-drift hypothesis left open in
[adk-2.7.1-dependency-refresh.md](./adk-2.7.1-dependency-refresh.md).*

**Answer up front.** Two defects, found in that order:

1. Turns that end on a `function_call` with no synthesized answer were never
   retried, so they reached the judges as an empty response.
2. **The bigger one:** `agent_data.agents` was `None` on every batch item, so the
   judge was explicitly told the agent had **no tools** — and graded a real tool
   call as a *contradictory* statement. That, not the emptiness itself, is what
   drove those items to 0.

Fixing (1) took `hallucination_v1` 0.42 → 0.66. Fixing (2) took it to **0.90-0.93**
and `tool_use_quality_v1` from a years-long 0.33-0.42 to **0.93** — and decoupled
hallucination from the empty rate entirely. Neither was model regression, judge
drift, or a rubric-template change.

The sections below are in discovery order; the empty-rate analysis is still correct
and still worth reading, but read the [SHIPPED](#shipped-and-it-was-the-bigger-lever-tell-the-judge-what-tools-exist)
section for the dominant cause.

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

## SHIPPED, and it was the bigger lever: tell the judge what tools exist

*Added 2026-08-21, after the retry fix above.*

The empty-response penalty turned out to be **mostly a symptom of a second
defect**, not of the emptiness itself. Re-read the judge's rationale:

> *"The context explicitly states that the agent **has no tools**, meaning it
> cannot execute any function calls, including the one presented."*

The item scored 0 not merely because the response was empty, but because the
rendered `function_call` was judged **contradictory against a declared inventory of
nothing**. `agent_data.agents` was `None` on every batch item, because
`build_agent_info()` was wired only into `simulated_eval` — the batch path passed a
resource-name string to `run_inference` and never an `AgentInfo`.

`_agent_info_for()` now supplies it, and the result is that hallucination
**decouples from the empty rate**:

| run | empty rate | `hallucination_v1` | `tool_use_quality_v1` |
| --- | --- | --- | --- |
| before, coordinator | 4% | 0.80 | 0.42 |
| before, coordinator | 22% | 0.66 | 0.38 |
| **after, coordinator** | **27%** | **0.93** | **0.93** |
| **after, coordinator** | **12%** | **0.90** | **0.94** |
| before, router | 0-8% | 0.74-0.89 | 0.28-0.48 |
| **after, router** | **0%** | **0.90** | **0.62** |

A 27% empty rate now scores 0.93 where 22% previously scored 0.66. All six
coordinator rubrics pass simultaneously for the first time in the recorded history.

`tool_use_quality_v1` moving 0.33-0.42 → 0.93 resolves the long-standing
false-negative in
[coordinator-tool-use-quality.md](./coordinator-tool-use-quality.md) at the root:
the judge was penalising an agent it had been told owned no tools. The router
crosses its 0.60 threshold for the first time.

Only the two inventory-dependent metrics moved. `instruction_following`
(0.63-0.64), `final_response_match` (0.76) and `safety` (1.00) stayed in their
existing bands — the effect is targeted, not blanket inflation.

### The rename is the load-bearing detail

Passing `agent_info` naively adds a **phantom second candidate**. `_get_candidate_name`
only *warns* on a name mismatch, but the auto-built `inference_configs` keys off
`agent_info.name`, so a name the dataset doesn't know makes the service run an
**extra inference pass** under it — a second series (`coordinator_agent/*` beside
`agent_engine_0/*`) with its own scores, measured at 1.0 across the board in one
run and 0.45 hallucination in another. `_agent_info_for` sets
`info.name = dataset.candidate_name`, which keeps exactly one candidate.

### Level shift — do not compare across it

`tool_use_quality_v1` and `hallucination_v1` both step up on 2026-08-21. The
monitored `agent_eval/tool_use_accuracy` series is unaffected
(`publish_offline_eval` overwrites it with the standalone `geap_tool_use` judge),
but `src/doe/harvest.py` and `analyze.py` read the raw batch metrics, so DOE and
bake-off reports spanning this date mix two scales. The new numbers are the correct
ones — the old ones were measured against a false premise.

## Original write-up: the option as it looked before shipping

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

**Since shipped** — the phantom candidate is avoided by aligning `agent_info.name`
to the dataset's candidate name. See the section above for the measured result.
