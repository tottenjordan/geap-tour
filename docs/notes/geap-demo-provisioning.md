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

1. **Model Armor templates + floor setting** (govern) —
   `bash scripts/setup_model_armor.sh` creates the `geap-workshop-prompt` /
   `geap-workshop-response` templates that `src/armor/config.py` references via
   `MODEL_ARMOR_PROMPT_TEMPLATE` / `MODEL_ARMOR_RESPONSE_TEMPLATE` (now stamped
   with `templateMetadata` so they log sanitize operations at INSPECT_ONLY).
   `deploy_agents.py:_build_config` bakes those env vars into the engine **only
   when set** — export them (or rely on the config defaults, which already point
   at `projects/hybrid-vertex/...`). Then
   `bash scripts/setup_model_armor_floor_settings.sh` configures the project's
   global floor setting (Vertex AI inspect-only + Cloud Logging) — this is what
   populates the console **Security-tab Model Armor dashboard**; see
   [model-armor-security-dashboard.md](./model-armor-security-dashboard.md) for
   the two caveats (custom-MCP ≠ Google-MCP; native-Gemini backbone for richest
   data).
2. **Cloud Logging — sink, viewer grant, stdout verification** —
   `bash scripts/setup_logging_sink.sh`. The managed Agent Runtime auto-routes
   each engine's stdout/stderr to the `reasoning_engine_stdout` /
   `reasoning_engine_stderr` log IDs on the
   `aiplatform.googleapis.com/ReasoningEngine` resource (no in-agent setup — see
   the [runtime logging doc](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/logging)),
   so the script's job is the pipeline + access around them. It: creates the
   BigQuery dataset `geap_workshop_logs` (`BQ_EVAL_DATASET`) and a sink filtered
   to the ReasoningEngine resource; grants **`roles/logging.viewer`** so operators
   can actually read the runtime logs in Logs Explorer / the Agent Runtime
   dashboard (default: the active gcloud account; override with
   `LOG_VIEWER_MEMBER=group:…`); and prints a `gcloud logging read` verification
   for `reasoning_engine_stdout` (set `AGENT_ENGINE_ID=<id>` to tail a specific
   engine). NOTE: continuous eval no longer requires a hand-created
   `online_eval_results` table — the canonical source is the native monitor's
   `agent_eval/*` metric series; the BQ table is an **optional** export sink only
   (`verify_monitors.py --source bigquery` returns `no_table` gracefully when
   absent). Cloud Logging does **not** cover Agent Runtime child resources
   (Sessions, Memory Bank, Code Execution, Example Store) — those are not logged.
3. **Monitoring workspace + alerts + dashboard** (observability) —
   - **Seed the metric descriptors first.** An alert policy cannot reference a
     custom metric type that has never had a TimeSeries written — Cloud
     Monitoring returns `404 Cannot find metric(s) that match type =
     custom.googleapis.com/agent_eval/helpfulness`. Write one placeholder point
     to each `agent_eval/*` **and** `agent_online_eval/*` series to materialize
     the descriptors:
     ```bash
     uv run python -c "from src.observability.metrics import write_quality_scores, write_online_quality_scores; from src.eval.quality_alerts import ALL_MONITORED_METRICS, ONLINE_MONITORED_METRICS; write_quality_scores({n: 5.0 for n,_ in ALL_MONITORED_METRICS}); write_online_quality_scores({n: 5.0 for n,_ in ONLINE_MONITORED_METRICS})"
     ```
     (New descriptors can take up to ~10 min to become queryable; in practice
     the alert create below usually succeeds within seconds.)
   - alert policies: `uv run python -m src.eval.quality_alerts all` — the `all`
     subcommand creates a policy for every metric across all three families:
     coordinator quality (`agent_eval/*`), online quality (`agent_online_eval/*`,
     the continuous client-side series), and router efficiency (`agent_router/*`).
     NOTE: bare `quality_alerts` with no arg only creates the single
     `helpfulness` policy.
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

## Periodic-snapshot eval (offline bridge, two surfaces)

```bash
uv run python -m src.eval.run_all_evals --skip-traffic     # publish both surfaces: agent_eval/* (coordinator) + agent_router/* (router)
uv run python -m src.eval.verify_monitors --format json    # read all three surfaces: coordinator_quality + online_quality + router_efficiency
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
4. **Continuous evaluation.** The native Vertex Online Evaluators are
   platform-blocked (the managed runtime strips prompt/response content from
   traces → `INSUFFICIENT_DATA`), so continuous eval runs **client-side**: the
   online monitor samples live coordinator traffic, scores each response with the
   same rubrics the offline bridge uses, and publishes a continuous
   `agent_online_eval/*` series (`eval_mode=online`) onto the SAME dashboard +
   1-5/3.0 alert surface as the offline `agent_eval/*` snapshot:
   ```bash
   uv run python -m src.eval.online_monitor --agent-id <ENGINE_ID>   # sample live traffic → agent_online_eval/*
   ```
   The dashboard's "Online Eval: *" widgets move in real time next to the offline
   snapshot; `verify_monitors` reads the new series as its `online_quality` block.
   See [online-quality-monitor.md](./online-quality-monitor.md).
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
6. **Build/Scale — Memory Bank.** *Pre-seed any time ahead* (direct creation is
   synchronous — no async lag to wait on):
   `uv run python -m src.eval.seed_demo_memories --engine-id 4380288848559603712`
   writes curated persona facts (alice/dana/sam) **directly** via
   `agent_engines.create_memory`, scoped by the engine id (the runtime's own
   `app_name`), so the console Memory Bank view is instantly rich and the
   coordinator's `PreloadMemoryTool` recalls them. It reads each persona back to
   confirm and exits non-zero if any failed (idempotent — re-running enriches, not
   duplicates). This deliberately skips the "organic" path (drive a session → the
   coordinator's `add_session_to_memory` → async distillation): that path is
   unreliable for a demo and persisted **zero** retrievable facts here — async lag,
   a `try/except: pass` in the callback, and (the real killer) an app_name scope
   mismatch. See [[memory-bank-app-name-scope]]. Then the money-shot is genuine
   cross-session recall:
   `uv run python -m src.eval.verify_cross_session_recall --user-id alice --engine-id 4380288848559603712`
   states a preference in session A, confirms the store, then opens a **brand-new
   session B** and shows the coordinator recall it (`RECALL: PASS`) via
   `PreloadMemoryTool` — not the live session's context window (the load
   generator's `CONVERSATIONS` stay in one session, so they don't prove this). Its
   probe is a pure-recall question ("remind me of my saved travel preferences") —
   a *booking* probe streams empty on the probe engine — and it retries an empty
   probe stream (`--probe-attempts`, default 3). Corroborate the store directly
   with `uv run python -m src.eval.verify_memory --user-id alice --engine-id 4380288848559603712`
   (also engine-scoped by default); the Memory Bank console view shows the same
   persisted facts. This demo targets the **probe engine `4380288848559603712`**
   (the same engine as the traffic + online-monitor run), not the pinned `.env`
   coordinator — pass `--engine-id` explicitly (the post-rollout regression crashes
   freshly-built engines, but the native-Gemini probe is healthy — see
   [[coordinator-outage-is-runtime-not-model]] and [[native-gemini-probe-engine]]).
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
