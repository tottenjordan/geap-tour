# Online-eval INSUFFICIENT_DATA — true root cause & the fix

**TL;DR** The native Vertex Online Evaluators returned `INSUFFICIENT_DATA` for
every cycle **not** because the managed runtime hard-strips content, but because
the runtime forces the ADK span-content gate **closed** unless the deployment is
built with `AdkApp(enable_tracing=True)`. That is the one lever. Setting it opens
the gate: `call_llm` spans then carry real `gcp.vertex.agent.llm_request`
(system_instruction + contents) and `llm_response`, and the evaluator can parse
them. Wired behind the opt-in `ENABLE_SPAN_CONTENT_CAPTURE` flag (default OFF).

This supersedes the prior standing conclusion ("hard platform limitation, no
lever from our side") recorded in memory `online-eval-content-capture-blocked`
and the earlier framing in
[offline-eval-monitoring-bridge](./offline-eval-monitoring-bridge.md).

## The content gate (ADK side)

`google.adk.telemetry.tracing.trace_call_llm` writes the prompt/response onto the
`call_llm` span **only when** `TelemetryConfig.should_add_content_to_legacy_spans`
is True; otherwise it writes the literal `gcp.vertex.agent.llm_request="{}"` /
`llm_response="{}"` (`tracing.py`). That property reads the env var
`ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS`, which **defaults to `'true'`** in local
ADK 2.6.3 (`telemetry/context.py`). So locally the gate is open by default — which
is exactly why every earlier "the env vars work locally but do nothing on the
runtime" experiment was so confusing.

## The true root cause (managed runtime side)

`vertexai/agent_engines/templates/adk.py` `set_up()` (≈ lines 944-950)
**hard-overwrites** `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` in `os.environ` at
container startup, deriving its value **solely** from the deprecated
`enable_tracing` template attribute — forcing `"false"` when `enable_tracing` is
absent/falsy. This clobbers whatever content-capture env var we baked into the
deployment spec. That single overwrite explains every prior null result:

1. **Deploy-spec env vars were clobbered at runtime.** We baked
   `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=true`, `ADK_TELEMETRY_IGNORE_RUN_CONFIG=1`,
   `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=SPAN_AND_EVENT`,
   `OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental` into `deployment_spec.env`
   and saw zero effect — because `set_up()` overwrites the load-bearing one after
   the container boots.
2. **The gate keys on `enable_tracing`, not the telemetry env var.** The newer
   `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true` path (which we set) enables
   trace **export** but does **not** open content capture — different switch.
3. **The "enable_tracing crashes the worker" belief was a confound.** `enable_tracing=True`
   was removed on 2026-08-14 (commit aac7d1a) because a deploy streamed 0 events —
   but that signature matches the concurrent Aug-13 platform outage that crashed
   ALL fresh engines (`coordinator-outage-is-runtime-not-model`) and no longer
   reproduces on the native-Gemini path.

## The fix

Deploy with `AdkApp(agent=…, enable_tracing=True)`. In this repo that is wired
behind an opt-in flag so normal deploys keep the privacy-preserving default:

- `src/config.py:ENABLE_SPAN_CONTENT_CAPTURE` (env `ENABLE_SPAN_CONTENT_CAPTURE=1`,
  default OFF).
- `src/deploy/deploy_agents.py:_build_app()` passes `enable_tracing=True` to
  `AdkApp` **only** when the flag is set.

```bash
# Deploy a coordinator whose call_llm spans carry real prompt/response content
ENABLE_SPAN_CONTENT_CAPTURE=1 uv run python -m src.deploy.deploy_agents coordinator --update
```

## Live validation (2026-08-15)

A throwaway `native-gemini-3.7-flash` coordinator engine was deployed with
`ENABLE_SPAN_CONTENT_CAPTURE=1`, driven with traffic, and its Cloud Trace
`COMPLETE`-view `call_llm` spans inspected:

- **46/46** `call_llm` spans carried a **non-empty** `gcp.vertex.agent.llm_request`
  (10-18 KB each) containing `system_instruction` + contents, and a non-empty
  `llm_response` — vs the `"{}"` seen on every default-deploy engine.
- The engine **streamed healthily** (probe PASS, events > 0) — refuting the
  "enable_tracing crashes the worker" attribution.
- The throwaway engine was torn down (`agent_engines.delete(..., force=True)`);
  no orphan left in us-central1.

## Two evaluator error paths (which this fixes)

- **Coordinator:** `system_instruction not present in any call_llm span` — the
  legacy **span** path (`gcp.vertex.agent.llm_request`). Fixed directly by
  `enable_tracing=True` opening the span-content gate above.
- **Router:** `Label 'gen_ai.system_instructions' is not present in any log entry`
  — the experimental **gen_ai log** path, a separate gate
  (`should_add_content_to_logs` via `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`).
  Not covered by this span fix; the router quality axis is served by the offline
  bridge (`agent_router/*`) either way.

## Why the offline bridge & client-side monitor still stand

The [offline-eval bridge](./offline-eval-monitoring-bridge.md) (`agent_eval/*`,
`agent_router/*`) and the [client-side online quality monitor](./online-quality-monitor.md)
(`agent_online_eval/*`) remain the **canonical shipped surfaces** — they are
model-neutral, need no privacy-off content capture on the served engine, and
already drive the dashboard/alerts. The native online path is now **unblockable
on demand** (native-Gemini backbone + `ENABLE_SPAN_CONTENT_CAPTURE=1`) for anyone
who wants managed Online Evaluators, but enabling it is a deliberate posture
choice (server-side prompt/response content lands in Cloud Trace), not a default.

## Honest caveats

- Validated on a **throwaway native-gemini-3.7-flash** engine, not the pinned
  production coordinator. Enabling it on the pinned engine is a separate, gated
  step.
- `enable_tracing=True` writes prompt/response content into Cloud Trace — a
  privacy trade-off; keep OFF unless managed Online Evaluators are actually wanted.
- Span content capture is the **native-Gemini** path; the router's gen_ai **log**
  path is a different gate and is not addressed here.
- Line numbers in the vendored `templates/adk.py` / ADK telemetry are for the
  versions pinned when this was written (vertexai 1.163.0, google-adk 2.6.3) and may
  drift — the repo is now on aiplatform 1.165.1 / google-adk 2.7.1.

Related: [[online-eval-content-capture-blocked]] (memory, now corrected),
[[coordinator-outage-is-runtime-not-model]] (memory),
[offline-eval-monitoring-bridge](./offline-eval-monitoring-bridge.md),
[online-quality-monitor](./online-quality-monitor.md),
[gemini3-native-model-resolution](./gemini3-native-model-resolution.md).
