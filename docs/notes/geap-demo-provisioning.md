# GEAP live-demo provisioning & runbook (hybrid-vertex)

The demo showcases all four GEAP pillars — build, scale, govern, optimize —
against the deployed coordinator + router, with synthetic traffic driving
observability, continuous evaluation, trace debugging, and governance blocks
that are all visible in the Cloud console. This note is the one-time
provisioning checklist plus the run-of-show. It complements the seven
demo-enhancement features (see `docs/plans/2026-08-12-geap-demo-enhancements.md`).

## Project

`src/config.py` already defaults `GCP_PROJECT_ID="hybrid-vertex"`,
`GCP_REGION="us-central1"`. The Model Armor templates + BigQuery logging sink
were originally provisioned in **wortz-project-352116**; the demo runs in
**hybrid-vertex**, so those artifacts must be re-created there (steps below).
`.env` is the source of truth for the deployed engine IDs and is auto-updated
by `deploy_agents.py` — re-deploy into hybrid-vertex and let it rewrite the
`AGENT_ENGINE_ID` / `ROUTER_ENGINE_ID` values (defaults in `config.py` are
stale placeholders).

## One-time provisioning (cheapest → richest)

1. **Model Armor templates** (govern) — `bash scripts/setup_model_armor.sh`.
   Creates `geap-workshop-prompt` / `geap-workshop-response` templates that
   `src/armor/config.py` references via `MODEL_ARMOR_PROMPT_TEMPLATE` /
   `MODEL_ARMOR_RESPONSE_TEMPLATE`. `deploy_agents.py:_build_config` bakes those
   env vars into the engine **only when set** — export them (or rely on the
   config defaults, which already point at `projects/hybrid-vertex/...`).
2. **BigQuery logging sink / dataset** — `bash scripts/setup_logging_sink.sh`.
   Dataset `geap_workshop_logs` (`BQ_EVAL_DATASET`). NOTE: continuous eval no
   longer requires a hand-created `online_eval_results` table — the canonical
   source is the native monitor's `agent_eval/*` metric series; the BQ table is
   an **optional** export sink only (`verify_monitors.py --source bigquery`
   returns `no_table` gracefully when absent).
3. **Monitoring workspace + alerts + dashboard** (observability) —
   - alert policies: `uv run python -m src.eval.quality_alerts` (policies on
     `custom.googleapis.com/agent_eval/*`, thresholds from `ALL_MONITORED_METRICS`).
   - dashboard-as-code: `uv run python -m src.observability.dashboard`
     (idempotent create/update; prints the console deep-link).
4. **Governance backups (preview-optional, skip if unavailable):**
   `bash scripts/setup_governance_policies.sh --sgp`,
   `bash scripts/setup_agent_gateway.sh`, `bash scripts/setup_agent_identity.sh`.
   These are the deeper govern story; the GA path (Model Armor + client-side
   guardrail) carries the demo without them.

## Deploy

```bash
uv run python -m src.deploy.deploy_agents all       # coordinator + router
```
Bakes: telemetry on (`GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=true`), Model
Armor template env, per-agent model/boundary env, and (coordinator only) the
Memory Bank + Session service builders (`_build_app` wraps memory-enabled agents
in `AdkApp(memory_service_builder=..., session_service_builder=...)`; the router
is excluded via `_wants_memory`).

## Continuous eval

```bash
uv run python -m src.eval.setup_online_evaluators create   # onlineEvaluator over coordinator+router
uv run python -m src.eval.setup_online_evaluators verify   # read native results + bridge → agent_eval/*
uv run python -m src.eval.verify_monitors --format json    # summarize the agent_eval/* series
```

## Run of show (the four money-shots)

1. **Scale / enabler — traffic.** Start the load generator so every board moves:
   ```bash
   uv run python -m src.traffic.generate_traffic <ENGINE_ID> \
     --load --qps 5 --duration 15 --ramp 60 --workers 8 --emit-metrics
   ```
   Ramp 0→5 QPS over 60s then hold; `--emit-metrics` writes `agent_traffic/*`.
2. **Observability.** Open the dashboard (deep-link from step 3 above) — latency
   p50/p95, QPS, error-rate, injected count, and `agent_eval/*` quality widgets
   move in real time; an alert can be tripped.
3. **Trace debugging.** Open a recent trace in Cloud Trace — the `router.route`
   span carries `complexity.score`, `routing.tier`, `model.id`, and the
   `boundaries.*`, explaining *why* a query went lite vs opus; coordinator spans
   carry `session.id`/`user.id` and ADK-emitted per-tool spans. Fallback:
   `uv run python -m src.observability.fetch_trace <TRACE_ID>`.
4. **Continuous evaluation.** The online monitor shows scored samples in the
   console; `verify` bridges scores onto the same `agent_eval/*` series the
   dashboard charts.
5. **Governance block.** Re-run traffic with injection:
   ```bash
   uv run python -m src.traffic.generate_traffic <ENGINE_ID> \
     --load --qps 3 --duration 5 --error-rate 0.3 --emit-metrics
   ```
   `INJECTED_QUERIES` include prompt-injection strings that match
   `BLOCKED_PATTERNS`; the coordinator's `guardrail_with_telemetry`
   `before_agent_callback` blocks them, emitting a `guardrail.blocked` span event
   + a `custom.googleapis.com/agent_armor/blocked` metric (visible on the board
   and in the trace).
6. **Build/Scale — Memory Bank.** The load generator's `CONVERSATIONS` establish
   a preference in one session; a later session recalls it. Prove it:
   `uv run python -m src.eval.verify_memory --user-id alice`. Memory Bank console
   view shows persisted facts.
7. **Build/Scale — A2A (preview-optional).**
   `uv run python -m src.deploy.register_a2a` registers the coordinator's agent
   card in Agent Registry; `--discover` lists A2A agents. If the preview surface
   is unavailable in hybrid-vertex, both log "A2A preview not enabled — skipping"
   and exit 0 — never crashing the demo.

## Verification (existence checks)

- `gcloud model-armor templates list --location=us-central1` → geap-workshop-*
- `bq ls hybrid-vertex:geap_workshop_logs` → dataset exists
- `gcloud monitoring dashboards list` → the code-defined dashboard present
- `gcloud alpha monitoring policies list` → `agent_eval/*` alert policies present
- deployed engines reachable via `agent_engines.get(<ENGINE_ID>)`

See also [Vertex eval pipeline](./vertex-eval-pipeline.md) and
[dependency management](./dependency-management.md).
