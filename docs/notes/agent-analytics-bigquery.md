# Agent-analytics content logging to BigQuery

**Status:** wired, **opt-in** (`ENABLE_AGENT_ANALYTICS=0` by default). The live
content-capture verification (below) is **pending a deploy** — do not assume rows
land until Task 1.4 is run and this note is updated with the result.

## Why this exists

The managed Agent Engine runtime **strips prompt/response/tool content from the
OTEL trace surface for every model**, not just LiteLlm-backed ones. It forces the
genai semantic-convention capture mode to `EVENT_ONLY` (non-span-bearing) and does
not run the `google-genai` OTel span instrumentor, so `call_llm` spans carry
`gcp.vertex.agent.llm_request={}` and `execute_tool` spans carry
`tool_call_args={}` regardless of backbone. That is why the native console Online
Evaluators always return `INSUFFICIENT_DATA` (see
[offline-eval-monitoring-bridge](./offline-eval-monitoring-bridge.md)), and why
switching Gemini off LiteLlm to the native global endpoint would **not** restore
span content — the limitation is in the runtime, not the model wrapper.

The **`BigQueryAgentAnalyticsPlugin`** (ships in `google-adk`) is a different,
runner-level path: it streams full LLM requests, responses, tool calls, and agent
responses straight to BigQuery via the Storage Write API, **independent of OTEL**.
It is model-neutral — it captures the same content for the Gemini and the
Claude(LiteLlm) coordinator — and each row carries the `trace_id`, so BigQuery rows
join back to Cloud Trace. This is the honest content-logging antidote for the
bake-off comparison.

## Wiring

- `src/config.py` — opt-in flags:
  - `ENABLE_AGENT_ANALYTICS` (default `0`) — master switch.
  - `BQ_AGENT_ANALYTICS_DATASET` (default `geap_agent_analytics`).
  - `AGENT_ANALYTICS_TABLE` (default `agent_events`).
- `src/deploy/deploy_agents.py`:
  - `_analytics_plugin()` builds the plugin (deferred import; returns `None` when
    disabled so the off-path and unit tests never touch the BQ client libs).
  - `_build_app()` passes `plugins=[plugin]` to `AdkApp` when enabled, for **every**
    agent (so both bake-off coordinators log). `AdkApp` forwards `plugins` to the
    Runner and deep-copies them on `clone()`, so they survive the deploy cycle.
  - `REQUIREMENTS` gains `google-cloud-bigquery-storage`, `google-cloud-storage`,
    `pyarrow` (transitive of the adk extra, pinned so they land in the served image).

## Prerequisites (run once, before Task 1.4)

The plugin runs inside the served engine as the **Agent Engine runtime service
account**, so grant it:

```bash
# Enable the Storage Write API (used by the plugin's streaming writer).
gcloud services enable bigquerystorage.googleapis.com --project "$GCP_PROJECT_ID"

# The runtime SA needs to create the dataset/table + write rows, and write large
# payloads to the staging bucket (offload path). Substitute the engine's SA.
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
  --member "serviceAccount:<AGENT_ENGINE_RUNTIME_SA>" \
  --role roles/bigquery.dataEditor
gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
  --member "serviceAccount:<AGENT_ENGINE_RUNTIME_SA>" \
  --role roles/bigquery.user   # bigquery.datasets.create / jobs
gsutil iam ch "serviceAccount:<AGENT_ENGINE_RUNTIME_SA>:roles/storage.objectAdmin" \
  "gs://${GCP_PROJECT_ID}-geap-staging"
```

## Task 1.4 — empirical content-capture gate (do before trusting this path)

Our history is that the runtime silently strips content, so prove rows land before
building any downstream analysis on this surface:

```bash
# Keep the bake-off engines UP — update in place, don't teardown/redeploy.
ENABLE_AGENT_ANALYTICS=1 uv run python -m src.deploy.deploy_agents coordinator --update
# → drive a little traffic, then:
#   SELECT * FROM `geap_agent_analytics.v_llm_request` ORDER BY ... LIMIT 20
#   confirm NON-EMPTY prompt/response content. Repeat for the Claude engine.
```

**Decision gate:** content present for both → roll out and wire downstream
(Workstream 2 can read real trajectories from BQ). Empty → STOP, record it here,
and do not build on it. Update memory `online-eval-content-capture-blocked` with the
outcome either way.

## Caveats

- Opt-in by design: default deploys don't require the BQ Storage Write API or the
  extra IAM. Turning it on adds Storage-Write-API billing.
- Batch/queue defaults (`batch_size=50`, `queue_max_size=10000`, `view_prefix="v_"`)
  are tuned for demo traffic; on overflow the plugin drops rows rather than blocking
  the request — treat the surface as best-effort telemetry, not an audit log.
