# Offline-eval → monitoring bridge (canonical quality source)

**Posture:** the native Vertex Online Evaluators are platform-blocked for our
agents, so the **offline-eval bridge** is the canonical source feeding the
`custom.googleapis.com/agent_eval/*` quality series (dashboard + alerts +
`verify_monitors`).

## Why the native online path is dead

The managed Agent Engine (ReasoningEngine) runtime does not emit
prompt/response/system_instruction content into the OTel trace/log path, so the
native `onlineEvaluator` cannot score predefined metrics — every cycle returns
`INSUFFICIENT_DATA`. This was verified empirically (google-adk 2.6.3):

- `gcp.vertex.agent.llm_request` / `llm_response` on every `call_llm` span are
  `{}`; the documented content-capture env vars
  (`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`,
  `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`,
  `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS`, `ADK_TELEMETRY_IGNORE_RUN_CONFIG`) are
  baked into the engine and have **zero effect**.
- The native-Gemini lever also failed (redeployed coordinator on plain
  `gemini-2.5-flash`, still `{}` spans, 0 `gen_ai.system_instructions` log hits) —
  the `opentelemetry-instrumentation-google-genai` instrumentor isn't active in
  the managed image, and `LiteLlm` bypasses it anyway.

Full root cause is in memory `online-eval-content-capture-blocked`. Conclusion:
no lever from our side unblocks it — hard platform limitation.

## What the bridge does instead

The batch / simulated / complexity evals already score the **deployed** engine
via the Vertex Gen AI Evaluation Service (`client.evals.run_inference(agent=…)`
+ `create_evaluation_run`) — Google's canonical "evaluate a deployed agent"
flow — but their scores only ever landed in local JSON. The bridge closes that
gap:

`src/eval/publish_offline_eval.py:publish_offline_scores()`
1. pulls the four monitored metrics from a `run_multi_agent_batch_eval` result
   + the complexity accuracy eval:

   | Monitored metric | Source | Scale in |
   |---|---|---|
   | `helpfulness` | coordinator batch `final_response_quality_v1` | 0-1 |
   | `tool_use_accuracy` | coordinator batch `tool_use_quality_v1` | 0-1 |
   | `policy_compliance` | coordinator batch `policy_compliance` (added to the coordinator metric set in `agent_eval_configs.get_metrics`) | 0-1 |
   | `complexity_routing_accuracy` | `run_complexity_accuracy_eval(ROUTER_EVAL_CASES)["accuracy"]` | 0-1 |

2. scales each `round(score * 5.0, 3)` onto the 1-5 monitored axis
   (`0.60 → 3.00`, the alert threshold);
3. tags every point `eval_mode=offline` and delegates to the shared
   `publish_eval_metrics()` (alias-maps to canonical names, filters to
   `ALL_MONITORED_METRICS` — non-monitored rubrics dropped, no drift, no scaling
   added on the shared path).

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

# Confirm the series is populated
uv run python -m src.eval.verify_monitors --format json   # expect status "ok", all four metrics 0-5
```

## Honest caveats

- **Snapshot, not per-request:** gauges are point-in-time per eval run, not
  continuous request telemetry — the `eval_mode=offline` label makes this
  explicit. `verify_monitors` 1h/6h/24h trends reflect sparse snapshots.
- **Native resource left in place:** the `onlineEvaluator` is harmless and kept
  for demo/discussion; it simply never produces usable scores.

Related: [[online-eval-content-capture-blocked]] (memory),
[coordinator-tool-use-quality](./coordinator-tool-use-quality.md).
