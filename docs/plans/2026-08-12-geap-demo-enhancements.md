# GEAP Demo Enhancements — Top 7 Additions to Showcase Build / Scale / Govern / Optimize

> **For Claude:** REQUIRED SUB-SKILL: use superpowers:executing-plans to implement this plan task-by-task.
> On execution, copy this file to `docs/plans/2026-08-12-geap-demo-enhancements.md`.

**Goal:** Make the repo a complete, live-demoable showcase of the Gemini Enterprise Agent Platform by wiring up the platform services that are currently *declared but unused* — so synthetic traffic flows into Cloud Trace, Cloud Monitoring, continuous evaluations, and governance blocks that are all visible in the console.

**Architecture:** Seven additive features, each closing a specific "wired-but-dead" gap found in the inventory. Nothing is rewritten; every feature turns an existing stub/dependency into a working platform integration. Priority is weighted to the four demo money-shots the user selected: **Observability & trace debugging, Continuous evaluation, Governance blocking, Build/Scale (A2A + Memory)**.

**Tech Stack:** ADK + Vertex AI Agent Engine (ReasoningEngine), OpenTelemetry → Cloud Trace, `google-cloud-monitoring` (custom metrics + dashboard), Vertex Gen AI eval service / online monitors, Model Armor, `a2a-sdk` + Agent Registry, `VertexAiSessionService` + `VertexAiMemoryBankService`. Demo project: **hybrid-vertex**.

**Feature posture (per user):** *Hybrid — GA core + preview optional.* Features 1–5 + 7 build on GA-guaranteed surfaces. Feature 6 (A2A) and the Agent Gateway / Semantic Governance backups are clearly flagged **preview-optional** and must degrade gracefully (skip with a logged notice, never crash the demo).

---

## Context

**Why now:** The next milestone is a **live customer demo** that must show *all* GEAP pillars — building, scaling, governing, optimizing — against a deployed agent, with synthetic traffic driving observability, evaluations, and trace debugging that are visible in the Cloud console.

**The problem the inventory surfaced:** the repo already *imports and declares* most GEAP services but leaves them dead-ended. Concretely (verified this session):

- **Traffic** (`src/traffic/generate_traffic.py`) is sequential/blocking — no real QPS, ramp, or concurrency — so it can't produce the traffic *shape* a live dashboard needs.
- **Monitoring** (`src/eval/quality_alerts.py`) creates alert policies on `custom.googleapis.com/agent_eval/*` but **nothing ever writes those TimeSeries**, and there is no dashboard-as-code.
- **Tracing** is env-flag-only for agents; explicit OTLP span code exists **only** for MCP servers (`src/mcp_servers/otel_setup.py`), so agent spans carry no demo-useful attributes (complexity score, chosen tier, tool latency).
- **Continuous eval** has two divergent half-built paths: `src/eval/setup_online_evaluators.py` (native onlineEvaluators → Cloud Logging) vs `src/eval/verify_monitors.py` (reads a BigQuery table `online_eval_results` that nothing creates).
- **Governance**: the deployed coordinator (`src/agents/coordinator_agent.py`) has **no guardrail wired**; the shared Model Armor config (`src/armor/config.py`) is never called from app code — so there is nothing to *show being blocked*.
- **Build/Scale**: `a2a-sdk` is a deploy dependency but there is **no A2A agent** (no `RemoteA2aAgent`/agent card); Agent Registry is used only for MCP discovery. Sessions/Memory Bank are minimal — `_memory_service_builder()` in `deploy_agents.py` is defined but unwired.
- **Project mismatch:** Model Armor templates + the BigQuery logging sink were provisioned in **wortz-project-352116**, not **hybrid-vertex** — the demo project needs its own provisioning.
- **Doc drift:** `CLAUDE.md:92` claims both coordinator and router import the shared armor module; only the router does.

Each of the seven features below turns one of these dead ends into a working, console-visible integration.

### Grounding facts (from exploration — do not re-derive)

- `generate_traffic.py` already has a rich `QUERIES` list (single-turn, complexity-tagged) and `CONVERSATIONS` (multi-turn Memory Bank exercises) plus `_send_single_query()`, `generate_steady_traffic()`, and a CLI. Reuse these; add concurrency + ramp + error-injection around them.
- OTel env plumbing lives in `src/config.py` (`OTEL_ENV_VARS`, `disable_pyopenssl()`); the MCP OTLP setup pattern to mirror is `src/mcp_servers/otel_setup.py`.
- Guardrail logic already exists (`input_guardrail_callback` in `src/armor/config.py`) and is triplicated elsewhere — consolidate onto the shared one rather than writing a fourth.
- Deploy env baking is `src/deploy/deploy_agents.py:_build_config` (`env_vars` dict); `_memory_service_builder()` is defined there but not passed to the deploy.
- Router boundary/threshold env overrides already exist in `src/config.py` (`COMPLEXITY_LOW/MEDIUM_SPLIT/COMPLEXITY_HIGH/HIGH_SPLIT`) — the router traffic already exercises all five tiers.
- Tests validate agent config offline (tool count, sub-agent names, callback presence) with no live GCP/MCP — every new feature needs matching offline tests to stay in the PR gate.

---

## Priority & phasing (money-shots first)

| # | Feature | Pillar / money-shot | Posture | Rough LOE |
|---|---------|--------------------|---------|-----------|
| 1 | Concurrent load generator (ramp + user pool + error injection) | Scale + enabler for **all** shots | GA | M |
| 2 | Custom metrics writer + dashboard-as-code | **Observability** | GA | M |
| 3 | Rich agent-side OTel spans | **Trace debugging** | GA | M |
| 4 | Consolidated continuous online evaluation | **Continuous evaluation** | GA | M–L |
| 5 | Model Armor + unified guardrail + block demo | **Governance blocking** | GA (SGP backup = preview) | M |
| 6 | A2A remote agent + Registry registration | **Build/Scale** | **Preview-optional** | L |
| 7 | Sessions + Memory Bank cross-session recall | **Build/Scale** | GA | M |

**Recommended demo build order:** 1 → 3 → 2 → 4 → 5 → 7 → 6. (Traffic first so every later feature has data; A2A last since it's preview-optional.) A minimal live demo is achievable with **1–5**; 6–7 deepen the Build/Scale story.

**Provisioning prerequisite (do once, before feature 4/5 live runs):** create/point Model Armor templates, the eval BigQuery dataset, and monitoring workspace in **hybrid-vertex**; update `.env` engine IDs. Track as Task 0.

---

## Task 0 — Provision hybrid-vertex + fix doc drift

**Files:** `.env`, `docs/notes/geap-demo-provisioning.md` (create), `CLAUDE.md`

- Document (and script where cheap) the hybrid-vertex provisioning: Model Armor prompt/response templates (mirror the wortz-project-352116 ones referenced in `src/armor/config.py`), the eval BigQuery dataset/table, and confirm the monitoring workspace. Reuse existing setup scripts under `scripts/` where present.
- Fix `CLAUDE.md:92` — only the router imports the shared armor module today (feature 5 makes it both; update the line to match reality *after* feature 5 lands, or note the pending state).
- **No test** (docs/provisioning). Verify by `gcloud`/console existence checks listed in the note.

---

## Feature 1 — Concurrent load generator (Scale; enables every money-shot)

**Files:** Modify `src/traffic/generate_traffic.py`; Test `tests/test_traffic_load.py` (create).

**What:** Add a concurrent, ramped load mode alongside the existing burst/steady modes. Reuse `QUERIES`, `CONVERSATIONS`, and `_send_single_query()`; do not duplicate the query corpus.

- Add `generate_load(agent, *, target_qps, duration_s, ramp_s=0, workers=8, error_injection=0.0, seed=None)` using a bounded `ThreadPoolExecutor` (Agent Engine `stream_query` is blocking I/O; threads give real concurrency without an async rewrite).
- **Ramp:** linearly increase offered QPS from 0→`target_qps` over `ramp_s`, then hold — this produces the rising latency/throughput curve that makes a dashboard "move" on camera.
- **User pool:** keep alice/bob/charlie; make the pool a parameter so it can grow.
- **Error injection:** with probability `error_injection`, send a deliberately malformed/oversized/policy-violating query (feeds feature 5's block demo and the error-rate metric). Tag injected queries so downstream metrics can separate them.
- **Determinism:** accept `seed` for reproducible demos via `random.Random(seed)`.
- CLI: add `--load --qps --duration --ramp --workers --error-rate --seed`; keep existing flags working.
- Emit a run summary (offered vs achieved QPS, p50/p95 latency, error count) to stdout for the demo narrator.

**Tests (offline, no GCP):** inject a fake `agent` with a recording `stream_query`; assert (a) concurrency actually overlaps (workers>1 → wall-clock < serial sum), (b) ramp schedule reaches target, (c) `error_injection=1.0` sends only tagged bad queries, (d) `seed` makes two runs identical.

---

## Feature 2 — Custom metrics writer + dashboard-as-code (Observability money-shot)

**Files:** Create `src/observability/metrics.py`, `src/observability/dashboard.py`; Modify `src/traffic/generate_traffic.py` (emit metrics) and reference `src/eval/quality_alerts.py` (the alert policies that already target these metric types); Tests `tests/test_metrics.py`, `tests/test_dashboard.py`.

**What:** Actually write the `custom.googleapis.com/agent_eval/*` (and new `agent_traffic/*`) TimeSeries the alert policies already expect, and define the dashboard as code so the demo opens a pre-built board.

- `metrics.py`: thin wrapper over `google.cloud.monitoring_v3` `create_time_series` — helpers `write_gauge(metric_type, value, labels)` and `write_distribution(...)`. Metric types: `agent_traffic/request_latency`, `.../error_rate`, `.../qps`, and per-tier `.../routed_cost_usd`; plus the `agent_eval/*` quality metrics feature 4 will populate. Resource type `generic_task` keyed by agent engine id + region.
- Wire feature 1's load summary to emit `agent_traffic/*` after each interval so the dashboard moves during the demo.
- `dashboard.py`: build a `google.cloud.monitoring_dashboard_v1` Dashboard proto (latency, QPS, error-rate, cost-by-tier, quality metrics) and create/update it idempotently. Print the console deep-link.
- Align metric-type strings with the ones `quality_alerts.py` already references so alerts fire on the same series.

**Tests (offline):** monkeypatch the monitoring client; assert the correct metric type/labels/resource are sent, and the dashboard proto contains the expected widgets. No real API calls in the PR gate.

---

## Feature 3 — Rich agent-side OTel spans (Trace-debugging money-shot)

**Files:** Create `src/observability/tracing.py`; Modify `src/agents/coordinator_agent.py`, `src/router/agents.py`, `src/router/complexity.py` (annotate), and `src/config.py` (ensure telemetry env on by default for demo); Tests `tests/test_tracing.py`.

**What:** Turn agent traces from "on but empty" into rich, debuggable spans. Mirror the MCP OTLP pattern in `src/mcp_servers/otel_setup.py`.

- `tracing.py`: `get_tracer()` + `@traced("span_name")` decorator/context-manager that adds span attributes and records exceptions. Safe no-op if OTel isn't configured (so tests and local runs don't need a collector).
- Instrument the **router**: a span per request carrying `complexity.score`, `routing.tier`, `model.id`, `boundaries.*` — so the trace explains *why* a query went to lite vs opus (the single best trace-debugging beat).
- Instrument the **coordinator**: spans around each MCP tool call (`tool.name`, latency, arg summary), delegation hops, and `session.id`/`user.id` attributes for correlation.
- Confirm `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY` is set by `deploy_agents.py:_build_config` (add to `env_vars` if missing) so deployed engines export to Cloud Trace.
- Add a small `src/observability/fetch_trace.py` helper (or extend an existing verify script) to pull a recent trace by id via the Cloud Trace API — proves spans landed, useful as a demo fallback if the console is slow.

**Tests (offline):** use an in-memory OTel span exporter; drive the router classifier + a stubbed coordinator tool call; assert spans exist with the expected attribute keys/values. Existing `test_router.py`/`test_coordinator.py` must stay green (decorator is transparent when OTel absent).

---

## Feature 4 — Consolidated continuous online evaluation (Continuous-evaluation money-shot)

**Files:** Reconcile `src/eval/setup_online_evaluators.py`, `src/eval/verify_monitors.py`, `src/eval/setup_online_monitors.py`; modify/replace to one path; Modify `src/eval/quality_alerts.py` (consume the same metric types); Tests `tests/test_online_eval.py`.

**What:** Collapse the two divergent half-built paths into **one** continuous-eval flow that samples live traffic, scores it, and surfaces results in the console — the bridge from synthetic traffic to visible evals.

- **Decision to confirm at execution:** prefer the **native Online Monitors / onlineEvaluators** surface (managed, console-native, GA) as the primary path; keep the BigQuery `online_eval_results` table as an *optional export sink* rather than a second source of truth. This removes the "table nothing creates" dead end in `verify_monitors.py`.
- Configure an online monitor over the deployed coordinator sampling a % of live sessions, running final-response + trajectory metrics; results visible in the console's evaluation view.
- Bridge to feature 2: write the resulting scores to `custom.googleapis.com/agent_eval/*` so `quality_alerts.py` policies fire and the dashboard shows quality alongside traffic.
- Rename/retire the misnamed one-shot `setup_online_monitors.py`; document the single supported command in `CLAUDE.md`.

**Tests (offline):** monkeypatch the eval/monitoring clients; assert the monitor/evaluator is created with the right agent target, sample rate, and metric set, and that scores route to the agreed metric types. Verify `verify_monitors.py` reads from the chosen source (no reference to a non-existent table).

---

## Feature 5 — Model Armor + unified guardrail + governance block demo (Governance money-shot)

**Files:** Modify `src/agents/coordinator_agent.py` (wire callback + armored config), `src/armor/config.py` (single source), `src/deploy/deploy_agents.py` (bake armor template env); consolidate the triplicated guardrail; Tests `tests/test_guardrail.py`, extend `tests/test_coordinator.py`.

**What:** Make governance *visible* — a prompt-injection / policy-violation query gets blocked, and the block is observable (span event + metric + audit log).

- Wire `input_guardrail_callback` (from `src/armor/config.py`) as the coordinator's `before_agent_callback` — today the coordinator has none. Remove the other two/three copies; import the shared one everywhere (also fixes the `CLAUDE.md:92` claim).
- Apply `get_armored_generate_config()` server-side Model Armor to the coordinator's model config so server-side screening is actually in the request path (currently defined, never called).
- Emit a span event + a `custom.googleapis.com/agent_armor/blocked` metric increment on each block (feeds features 2 & 3 so the block shows up on the dashboard and in the trace).
- Feature 1's `--error-rate` injects the exact prompt-injection strings `BLOCKED_PATTERNS` matches, so the demo can *cause* a block on cue.
- **Preview-optional backup:** note (do not require) Semantic Governance Policies + Agent Gateway ingress as the deeper govern story; guard behind a flag that logs "SGP preview not enabled, using Model Armor + callback" when off.

**Tests (offline):** injection/oversized inputs → rejection Content; clean inputs → None (pass-through); coordinator now exposes `before_agent_callback`; block path increments the metric (monkeypatched). Keep armor calls mockable so no live template is needed in the gate.

---

## Feature 6 — A2A remote agent + Agent Registry registration (Build/Scale — PREVIEW-OPTIONAL)

**Files:** Create `src/a2a/agent_card.py`, `src/a2a/remote_agent.py`, `src/deploy/register_a2a.py`; Modify `src/registry.py` (register/discover A2A agents, not just MCP); Tests `tests/test_a2a.py`.

**What:** Turn the declared-but-unused `a2a-sdk` into a real, discoverable A2A agent so the demo shows agent-to-agent interop + Registry cataloging.

- Publish an **agent card** for the coordinator (name, skills, endpoints) and expose it via the A2A protocol.
- Add a `RemoteA2aAgent` client path so one agent can call the deployed coordinator over A2A.
- Register the A2A agent in **Agent Registry** alongside the existing MCP-server registrations in `src/registry.py`.
- **Graceful degradation (required):** wrap all A2A calls so that if the preview surface is unavailable in hybrid-vertex, the feature logs a clear "A2A preview not enabled — skipping" and the rest of the demo continues. Never let this crash a live run.

**Tests (offline):** agent card serializes with expected skills; `RemoteA2aAgent` builds against a stubbed endpoint; registry registration is invoked with the right resource name; the unavailable-preview path degrades to a logged skip, not an exception.

---

## Feature 7 — Sessions + Memory Bank cross-session recall (Build/Scale money-shot)

**Files:** Modify `src/deploy/deploy_agents.py` (wire `_memory_service_builder()` + a session service into the deploy), confirm `src/agents/coordinator_agent.py` `save_memories_callback`/`PreloadMemoryTool` path; Tests `tests/test_memory_wiring.py`.

**What:** Make cross-session recall real and demoable. The coordinator already has `save_memories_callback` (writes) + `PreloadMemoryTool` (reads); the deploy just never wires the services.

- Pass `VertexAiMemoryBankService` (via the existing `_memory_service_builder()`) and a `VertexAiSessionService` into the Agent Engine deploy so memories persist across sessions.
- Reuse feature 1's `CONVERSATIONS` (already designed to establish then recall preferences) as the demo script: turn 1 states a preference in one session, a later session recalls it — visible in the Memory Bank console view.
- Verify events are persisted and retrieved (add a small `src/eval/verify_memory.py` or extend an existing verify script to read back a user's memories).

**Tests (offline):** monkeypatch the deploy so `_build_config`/deploy call includes the memory + session services; assert the coordinator retains `save_memories_callback` + `PreloadMemoryTool`. No live Memory Bank needed in the gate.

---

## Verification

**Offline (PR gate, no GCP) — after each feature:**
`uv run pytest -q` (and `--group pipelines`/`--group doe` where relevant) must stay green. Each feature ships with offline tests (fakes/monkeypatch for all GCP clients) per the repo convention that tests validate config/behavior without live GCP or MCP. Target: full suite green (currently 294) + new tests.

**On GCP (staged, cheapest → richest), all in hybrid-vertex:**
1. **Provision (Task 0):** templates, eval dataset, dashboard exist; `.env` engine IDs point at hybrid-vertex.
2. **Deploy** coordinator + router with the new env baked (`deploy_agents.py`): telemetry on, armor templates, model/boundary vars.
3. **Smoke traffic:** `uv run python -m src.traffic.generate_traffic <engine> --load --qps 2 --duration 2 --ramp 1` → runs, prints achieved-QPS/latency summary, no crash.
4. **Observability money-shot:** open the code-defined dashboard → latency/QPS/error/cost widgets move; a `agent_eval/*` alert can be triggered.
5. **Trace money-shot:** open a recent trace in Cloud Trace → router span shows `complexity.score` + chosen `routing.tier` + model; coordinator spans show per-tool latency. `fetch_trace.py` pulls the same trace by id.
6. **Continuous-eval money-shot:** online monitor shows scored samples in the console; scores appear as `agent_eval/*` series on the dashboard.
7. **Governance money-shot:** `--error-rate 0.3` load run → injection queries are blocked; block appears as a span event + `agent_armor/blocked` metric on the dashboard.
8. **Build/Scale:** Memory Bank console shows persisted user memories; a second-session query recalls a first-session preference. (A2A, if preview enabled) coordinator agent card is discoverable in Agent Registry and callable via `RemoteA2aAgent`; if not enabled, the run logs a clean skip.

**Success =** synthetic traffic drives all four money-shots live in the hybrid-vertex console (observability, trace debugging, continuous eval, governance block) with the Build/Scale (Memory + A2A-if-available) story on top — and every feature has offline tests keeping the PR gate green.

---

## Risks & decisions

- **Preview volatility:** A2A / Agent Gateway / Semantic Governance are preview — feature 6 and the govern backups **must** degrade gracefully (logged skip), never crash a live demo. GA core (1–5, 7) carries the demo on its own.
- **Cost:** feature 1 can generate real inference load × N engines. Default to low `--qps`/short `--duration`; the summary line surfaces spend. Fresh deploys are the heaviest path — reuse the existing engine where possible.
- **Metric/eval source-of-truth:** feature 4 deliberately picks the native Online Monitor surface as primary and demotes the BigQuery table to an optional sink — confirm at execution before deleting the table-reader path.
- **Project migration:** artifacts live in wortz-project-352116 today; Task 0 must re-provision in hybrid-vertex before any live money-shot.
- **Doc drift:** update `CLAUDE.md:92` (shared armor module) as part of feature 5, not before.
- **Guardrail duplication:** consolidate onto `src/armor/config.py`'s `input_guardrail_callback` — do not add a fourth copy.
