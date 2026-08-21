# Scoring the tool trajectory: what the calibration found

*Implemented 2026-08-21. Closes the caveat in
[gepa-sampler-case-audit.md](./gepa-sampler-case-audit.md) — "no trajectory metric is
scored" — for the **eval** path, and explains why the **optimizer** half is
deliberately deferred.*

## Headline

The coordinator's tool ordering is **already perfect**. Measured over all 38
coordinator cases carrying a `reference_trajectory`, on the live probe engine:

```
miss breakdown (IN_ORDER, normalized):
    12  empty trajectory  (infra-empty turn, not an ordering error)
     0  genuine mismatch
  IN_ORDER over turns that produced ANY tool call: 100%
```

Every failure is an empty turn — the infra-empty problem from
[offline-eval-empty-turns.md](./offline-eval-empty-turns.md) — not a wrong or
misordered call. A headline trajectory score of 68% therefore means "68% of turns
produced tools at all", not "68% of orderings were right".

## Why the metric read zero before

Trajectory scoring compares tool calls **literally**. ADK's `TrajectoryEvaluator`
checks `actual.name == expected.name` **and** `actual.args == expected.args` in *all
three* match types (`trajectory_evaluator.py:203,239,272`). There is no normalization
hook.

Our two sides disagreed on naming:

| side | form |
| --- | --- |
| runtime `function_call` events | `booking_mcp_book_flight` — **32 of 33 calls observed** |
| `reference_trajectory`, evalset `tool_uses` | `book_flight` |

The prefix comes from `registry.get_mcp_tools` resolving through **Agent Registry**;
the direct-URL **fallback** yields bare names. That fallback was the normal path until
the 2026-08-15 IAM remediation, so `trajectory_eval.py`'s docstring — asserting
references use "the same names that appear in `stream_query` `function_call` events" —
*was true when written* and silently stopped being true. Corrected.

Measured effect, same 38 cases:

| match type | raw names | normalized |
| --- | --- | --- |
| `EXACT` | **0%** | 50% |
| `IN_ORDER` | **0%** | **68%** |
| `ANY_ORDER` | **0%** | 63% |

One call out of 33 came back **bare**, so the naming isn't even consistent within a
single run. Normalization is mandatory, not a convenience.

## `run_trajectory_eval` was dead for *three* reasons

It was fully written and unit-tested and called by nothing. Wiring it in exposed two
more defects beyond the naming, each of which alone pins the metric at zero:

1. **Names** (above) — 0% raw.
2. **Concurrent fan-out + an API that rejects empties.** `EvalTask.evaluate(runnable=…)`
   generates predictions itself, in a `ThreadPoolExecutor` — the documented trigger
   for empty-at-200 turns on a busy engine (the same reason `_sdk_patches` throttles
   the batch path). And the evaluation API does not score an empty
   `predicted_trajectory` as 0; it **rejects the row**: `Required field is not set`.
   Measured: `failure/mean 1.0` and every metric `nan`.
3. **Argument granularity.** The metrics compare the whole `{tool_name, tool_input}`
   dict, but `reference_trajectory` is a list of tool *names* — `_reference_trajectory`
   materialises `tool_input={}`. Leaving real args on the predicted side scored
   **0.00 on exact_match, precision and recall** for turns whose tool sequence was in
   fact correct.

Fixes, in `trajectory_eval.py`:

* `CoordinatorRunnable` gained the **raw-SSE fallback** (every other stream consumer
  in the repo already had it — memory `agent-engine-sse-parse-skew`) and a bounded
  **empty-turn retry**.
* `run_trajectory_eval` now generates predictions **serially, itself**, partitions out
  the turns that produced no tool call, and scores the rest in **bring-your-own-response**
  mode. Empties are reported as `empty_trajectories`, never folded into the mean —
  the same treatment the batch eval gives `infra_empty_rate`.
* Args are blanked on **both** sides, making this deliberately a **name-and-order**
  metric. Argument fidelity is covered elsewhere (`tool_faithfulness`, the
  `geap_tool_use` rubric).

Measured after, 10 coordinator cases on the probe engine:

```
scored_cases      : 7
empty_trajectories: 3
  trajectory_exact_match/mean   1.0
  trajectory_precision/mean     1.0
  trajectory_recall/mean        1.0
```

1.0 across the board, consistent with the calibration's "100% on turns that produced
any tool call".

## What shipped

- **`normalize_tool_name`** (`src/eval/trajectory_eval.py`) strips a leading
  `<domain>_mcp_` only when the domain is a real server *and* the remainder is one of
  that domain's tools, per `verify_mcp_tools.EXPECTED_TOOLS`. Anything else passes
  through — a normalizer that guesses would silently rename an unrelated tool.
  Applied in `CoordinatorRunnable.query`, the one place we own the comparison input.
  `extract_trajectory` still returns **raw** names, because `tool_faithfulness` shows
  the judge real tool names.
- **`run_trajectory_eval` is wired into `run_all_evals`** as Phase 4 of 8, placed
  *before* the `--batch-only` early return (it is an offline metric). It prints
  `scored_cases` beside the means — 38 of 49 is not a mean over 49.
- **`src/eval/calibrate_trajectory.py`**, the read-only diagnostic that produced every
  number above. Re-run it after any prompt, backbone or registry change.

Deliberately **not** published to Cloud Monitoring: `agent_eval/*` names are pinned by
`quality_alerts.ALL_MONITORED_METRICS`, and adding an alerting series is its own
decision.

## Why `IN_ORDER`, if we ever enable it

`IN_ORDER` requires the expected calls in order and tolerates extras; `EXACT` forbids
extras entirely. Our audit cases legitimately make one `check_expense_policy` per
category, so `EXACT` penalises the agent for being *more* thorough — visible above as
the 50% vs 68% gap. **`match_type` defaults to `EXACT` when omitted**, so a bare float
threshold silently buys the strictest comparison; a guard test now requires any config
opting into `tool_trajectory_avg_score` to spell it out.

Verified that the JSON round-trip works: `EvalConfig` parses
`{"match_type": "IN_ORDER", "threshold": 0.7}` as a `BaseCriterion` (`extra="allow"`)
and `TrajectoryEvaluator.__init__` re-validates it into `ToolTrajectoryCriterion`,
yielding `MatchType.IN_ORDER`.

## Deferred: adding it to the GEPA objective

The plan's decision gate was "≥ 0.6 → enable". It reads 68%, but the breakdown makes
the gate the wrong question, and **three findings argue against enabling it now**:

1. **The measurement is from the wrong path.** These numbers come from the *deployed*
   engine. GEPA runs the `_opt` sandbox agents **locally**, and ADK compares names
   internally — our normalizer cannot reach that comparison, only the evalset data
   can. If the local run emits prefixed names while the evalsets are bare, the
   criterion scores **0** and would actively mislead the optimizer. Unmeasured.
2. **Args are structurally uncomparable today.** `reference_trajectory` carries names
   only (`_reference_trajectory` materialises `tool_input={}`), and the optimizer
   evalsets' `tool_uses` carry real args that ADK compares exactly. Those are two
   different fidelity levels for the same idea.
3. **32% of turns are empty**, and a prompt cannot fix an empty-at-200 stream. On the
   7-case expense evalset that is noise the optimizer would chase.

**What unblocks it:** run `LocalEvalSampler` over one optimizer evalset and dump the
emitted tool names and empty rate — the local analogue of `calibrate_trajectory`. If
local names are prefixed, rewrite the 9 evalsets' `tool_uses` names to match *before*
adding the criterion. Then enable on `expense_sampler_config.json` alone, re-optimize,
and diff the produced instruction against the current one.

Note the incumbent prompt scores 100% on every scorable turn, so the criterion offers
no headroom on it — its value would be discriminating *bad candidate* prompts during
optimization, which is a real but weaker justification than the headline number
suggests.

## Known dependency

`run_trajectory_eval` uses `vertexai.preview.evaluation.EvalTask`. It imports fine on
aiplatform 1.165.1, but it is a **preview** surface and the repo has otherwise
migrated its client to `agentplatform`
([agentplatform-client-migration.md](./agentplatform-client-migration.md)). If it
disappears, the metric goes with it.
