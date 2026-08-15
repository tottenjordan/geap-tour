# Offline-eval → monitoring bridge (canonical quality source)

**Posture:** the **offline-eval bridge** is the canonical source feeding two
monitored series — `custom.googleapis.com/agent_eval/*` (coordinator quality,
1-5) and `custom.googleapis.com/agent_router/*` (router efficiency, native
units) — that drive the dashboard, alerts, and `verify_monitors`. It is
model-neutral and needs no privacy-off content capture on the served engine.

## Why the native online path returned INSUFFICIENT_DATA (and how it's now fixable)

By default the managed Agent Engine runtime emits **empty** prompt/response
content into the OTel span path, so the native `onlineEvaluator` cannot score
predefined metrics — every cycle returns `INSUFFICIENT_DATA`. Verified
empirically (google-adk 2.6.3):

- `gcp.vertex.agent.llm_request` / `llm_response` on every `call_llm` span are
  `{}`; the documented content-capture env vars
  (`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`,
  `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`,
  `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS`, `ADK_TELEMETRY_IGNORE_RUN_CONFIG`)
  baked into the engine have **zero effect** *from env*.

**Corrected root cause (2026-08-15):** this is **not** a hard platform strip. The
managed `AdkApp` runtime's `set_up()` hard-overwrites
`ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` at container start based **solely** on the
deprecated `enable_tracing` template flag — clobbering the deploy-spec env var.
Deploying with `AdkApp(enable_tracing=True)` opens the gate: `call_llm` spans then
carry real content (validated live — 46/46 spans with `system_instruction` +
contents). Wired behind the opt-in `ENABLE_SPAN_CONTENT_CAPTURE` flag (default
OFF, since it writes prompt/response into Cloud Trace). Full write-up in
[online-eval-content-capture](./online-eval-content-capture.md) and memory
`online-eval-content-capture-blocked` (corrected). The offline bridge below
remains the canonical shipped surface **by choice** — the native path is now
unblockable on demand, not a dead end.

## What the bridge does instead

The batch / simulated / complexity evals already score the **deployed** engine
via the Vertex Gen AI Evaluation Service (`client.evals.run_inference(agent=…)`
+ `create_evaluation_run`) — Google's canonical "evaluate a deployed agent"
flow — but their scores only ever landed in local JSON. The bridge closes that
gap:

There are **two honest surfaces**, because the coordinator (a task executor) and
the 5-tier router (an economic optimizer) are architecturally different agents
and must not be scored on the same axis:

### Coordinator quality → `custom.googleapis.com/agent_eval/*` (1-5 rubric, alert `< 3.0`)

`src/eval/publish_offline_eval.py:publish_offline_scores()`
1. pulls the three monitored quality metrics from a `run_multi_agent_batch_eval`
   result:

   | Monitored metric | Source | Scale in |
   |---|---|---|
   | `helpfulness` | coordinator batch `final_response_quality_v1` | 0-1 |
   | `tool_use_accuracy` | coordinator batch `tool_use_quality_v1` | 0-1 |
   | `policy_compliance` | coordinator batch `policy_compliance` (scored by the standalone `policy_judge` via `publish_offline_eval._inject_policy_compliance`, NOT `get_metrics` — the custom pointwise `LLMMetric` is SDK-broken) | 0-1 |

2. scales each `round(score * 5.0, 3)` onto the 1-5 monitored axis
   (`0.60 → 3.00`, the alert threshold);
3. tags every point `eval_mode=offline` and delegates to the shared
   `publish_eval_metrics()` (alias-maps to canonical names, filters to
   `ALL_MONITORED_METRICS` — non-monitored rubrics dropped, no drift, no scaling
   added on the shared path).

### Router efficiency → `custom.googleapis.com/agent_router/*` (native units)

`src/eval/publish_router_efficiency.py:publish_router_efficiency()` publishes the
router's economic metrics **verbatim in native units** (no `×5` scaling), from
the complexity accuracy + cost-efficiency evals:

   | Monitored metric | Source | Unit | Alert |
   |---|---|---|---|
   | `routing_accuracy_pct` | `run_complexity_accuracy_eval(ROUTER_EVAL_CASES)["accuracy"]` ×100 | % | `< 80.0` |
   | `cost_savings_pct` | `run_cost_efficiency_eval(...)["savings_pct"]` (verbatim) | % | `< 50.0` |
   | `classifier_latency_ms` | `run_complexity_accuracy_eval(...)["avg_latency_ms"]` | ms | `> 8000.0` |

`cost_savings_pct` (savings vs an all-Opus baseline) was previously computed and
discarded — it is now a first-class monitored series. `run_all_evals` publishes
both surfaces as its Phase 6 publish step.

## How to run it

```bash
# Reuse an existing run's artifacts (no engine cost) — default
uv run python -m src.eval.publish_offline_eval --latest
uv run python -m src.eval.publish_offline_eval --from-json <full_results.json|batch_results_*.json>

# Fresh: one coordinator run_inference + local complexity eval, then publish
uv run python -m src.eval.publish_offline_eval --run

# Preview without writing to Cloud Monitoring
uv run python -m src.eval.publish_offline_eval --dry-run --latest

# run_all_evals runs this automatically as a phase (Phase 6/7), so the
# monitor-verification phase then reads freshly-written points:
uv run python -m src.eval.run_all_evals --skip-traffic

# Confirm both surfaces are populated
uv run python -m src.eval.verify_monitors --format json   # two blocks: coordinator_quality (1-5) + router_efficiency (native units)
```

## Honest caveats

- **Snapshot, not per-request:** gauges are point-in-time per eval run, not
  continuous request telemetry — the `eval_mode=offline` label makes this
  explicit. `verify_monitors` 1h/6h/24h trends reflect sparse snapshots.
- **Native resource removed:** the `onlineEvaluator` setup + its deprecation shim
  were deleted (they returned `INSUFFICIENT_DATA` under the default deploy). The
  offline-eval bridge is the canonical quality source; the native online path is
  now unblockable on demand via `ENABLE_SPAN_CONTENT_CAPTURE=1` (see
  [online-eval-content-capture](./online-eval-content-capture.md)) but is not the
  shipped default.

Related: [[online-eval-content-capture-blocked]] (memory),
[coordinator-tool-use-quality](./coordinator-tool-use-quality.md).
