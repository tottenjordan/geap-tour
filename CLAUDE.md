# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GEAP Workshop: a hands-on demo of the Gemini Enterprise Agent Platform — ADK agents with MCP tools, multi-model routing, evaluation, optimization, and governance on Google Cloud. The domain is corporate travel and expense management.

## Commands

```bash
# Install
uv sync

# Run all tests
uv run pytest tests/

# Run a single test file / test
uv run pytest tests/test_router.py
uv run pytest tests/test_router.py::TestComplexityScoring::test_low_score

# Run MCP servers locally (each in its own terminal)
uv run python -m src.mcp_servers.search.server   # :8001
uv run python -m src.mcp_servers.booking.server   # :8002
uv run python -m src.mcp_servers.expense.server   # :8003

# Deploy agents to Agent Runtime
uv run python -m src.deploy.deploy_agents router
uv run python -m src.deploy.deploy_agents coordinator
uv run python -m src.deploy.deploy_agents all
uv run python -m src.deploy.deploy_agents router --update   # update existing

# Deploy MCP servers to Cloud Run
uv run python -m src.deploy.deploy_mcp_servers

# Run GEPA prompt optimization
uv run python -m src.optimize.run_optimize src/agents/coordinator
uv run python -m src.optimize.run_optimize src/router --sampler-config src/optimize/router_sampler_config.json

# Evaluation
uv run python -m src.eval.simulated_eval --agent-id <ENGINE_ID> --agent-name coordinator_agent
uv run python -m src.eval.multi_agent_batch_eval coordinator_agent
uv run python -m src.eval.run_all_evals

# Periodic-snapshot eval — offline bridge is the canonical source (native online path platform-blocked)
uv run python -m src.eval.publish_offline_eval --latest       # bridge newest coordinator quality scores → agent_eval/* (no engine cost)
uv run python -m src.eval.publish_offline_eval --run          # fresh coordinator batch, then publish
uv run python -m src.eval.publish_router_efficiency --from-json <full_results.json>  # router efficiency → agent_router/* (native units)
uv run python -m src.eval.verify_monitors --format json       # summarize both surfaces: coordinator_quality + router_efficiency

# CI/CD eval gate — fast coordinator rubric score for a PR (advisory; --limit caps cases)
uv run python -m src.eval.multi_agent_batch_eval --agents coordinator_agent --agent-id <ENGINE_ID> --threshold 3.0 --limit 8

# Content logging to BigQuery (opt-in; model-neutral, independent of the stripped OTEL surface)
ENABLE_AGENT_ANALYTICS=1 uv run --group analytics python -m src.deploy.deploy_agents coordinator --update  # wire BigQueryAgentAnalyticsPlugin (needs analytics group locally); see docs/notes/agent-analytics-bigquery.md

# Vertex Managed Pipeline (runs the full eval DAG on Vertex Pipelines)
bash scripts/build_eval_image.sh v1                                  # build+push runner image (one-time)
uv run python -m src.pipelines.submit --agent-id <ENGINE_ID> --skip-traffic   # reuse engine (fastest)
uv run python -m src.pipelines.submit --agent-module coordinator_agent        # full parity, fresh temp deploy

# DOE experiments (design → fan-out one PipelineJob per point → harvest → main-effects report)
uv run --group doe python -m src.doe.run_doe --kind screening               # dry-run design + cost estimate
uv run --group doe python -m src.doe.run_doe --kind screening --execute --wait

# Coordinator model bake-off — Gemini vs Claude (2 persistent deploys, own lifecycle; auto-teardown)
uv run --group doe python -m src.doe.run_bakeoff                            # dry-run plan (deploys/spends nothing)
uv run --group doe python -m src.doe.run_bakeoff --execute                  # deploy both → offline + cost + pairwise + traffic → report → teardown
uv run --group doe python -m src.doe.run_bakeoff --execute --keep-engines   # same, but leave both engines running afterwards
uv run python -m src.eval.pairwise_eval --from-manifest doe_runs/<exp>/manifest.json --dry-run  # pairwise SxS win-rate
uv run python -m src.eval.verify_monitors --format json --group-by model    # two online series: gemini vs claude

# A2A agent card — register / discover the coordinator in Agent Registry (preview-optional)
uv run python -m src.deploy.register_a2a              # publish the coordinator's agent card
uv run python -m src.deploy.register_a2a --discover   # list A2A agents in the registry

# Infrastructure setup (shell scripts)
bash scripts/deploy_all.sh              # full end-to-end
bash scripts/setup_governance_policies.sh --sgp   # IAM + SGP
bash scripts/setup_model_armor_floor_settings.sh  # project floor setting (inspect-only + Cloud Logging) → Security-tab Model Armor dashboard
```

## Architecture

### Two agent topologies

1. **Coordinator agent** (`src/agents/coordinator_agent.py`) — domain router with two sub-agents: `travel_agent` (flights/hotels via search + booking MCP) and `expense_agent` (policy checks/submissions via expense MCP). Uses `AgentTool` for delegation and `PreloadMemoryTool` for Memory Bank.

2. **Multi-model router** (`src/router/agents.py`) — 5-tier complexity router. A `before_agent_callback` calls the complexity classifier (`src/router/complexity.py`) which scores prompts 0–1, then routes to one of five sub-agents: lite (gemini-3.1-flash-lite) → flash (gemini-3.5-flash) → pro (gemini-3.1-pro-preview) → sonnet (claude-sonnet-4-6) → opus (claude-opus-4-6). The router itself runs on the lite model. Score boundaries (defaults, DOE-tuned for cost savings in screening doe-screening-20260812-073603): <0.44 lite, 0.44–0.60 flash, 0.60–0.80 sonnet, 0.80–0.95 pro, ≥0.95 opus. All four cut-points are env-overridable (`COMPLEXITY_LOW`/`MEDIUM_SPLIT`/`COMPLEXITY_HIGH`/`HIGH_SPLIT`).

### MCP server registration

Agents connect to MCP servers through two mechanisms in `src/registry.py`:
- **Agent Registry** (primary): `get_mcp_tools(server_name)` looks up the server by its registered resource name via `AgentRegistry.get_mcp_toolset()`.
- **Direct URL** (fallback): on any registry failure (`RuntimeError` control-plane error *or* `ValueError` missing endpoint URI) it falls back to the Cloud Run URLs in the `MCP_SERVER_URLS` mapping (`src/config.py`), logging the fallback at **WARNING** with the underlying error — a coordinator quietly running on the fallback path is exactly the silent degradation we want visible. Inside the managed Agent Engine runtime the engine runs under a per-engine `AGENT_IDENTITY`; registry resolution used to fall back on every request because that identity lacked `agentregistry.mcpServers.get` (a 403 wrong-principal IAM denial, **not** an unreachable API and **not** fixed by granting the RE service agent). **Remediated (2026-08-15):** grant `roles/agentregistry.viewer` to the engine's `principal://<effectiveIdentity>` (reproducible as "Step 0b" in `scripts/setup_governance_policies.sh`), then recycle cached toolsets with an in-place `deploy_agents … --update` (toolsets resolve once per container; a *recreate* mints a new identity and needs a fresh grant). The MCP servers also run **stateless HTTP** so Cloud Run scaling can't drop a session ("Session terminated" 404). See [docs/notes/agent-registry-mcp-resolution.md](docs/notes/agent-registry-mcp-resolution.md).

The three required env vars `SEARCH_MCP_SERVER`, `BOOKING_MCP_SERVER`, `EXPENSE_MCP_SERVER` hold Agent Registry resource names — these are NOT optional and will crash on import if missing. Verify all three toolsets actually resolve their real tools (turns a silent tool-less agent into a non-zero exit): `uv run python -m src.eval.verify_mcp_tools [--json]`.

### A2A (Agent-to-Agent) — preview-optional

`src/a2a/` makes the coordinator a discoverable A2A agent. `agent_card.py:build_agent_card()` builds an `a2a.types.AgentCard` (name `coordinator_agent`, five skills mirroring its real tools: flight/hotel search, booking, expense policy check, expense submission) whose endpoint derives from `config.coordinator_a2a_url()`; `agent_card_dict()` serializes it. `remote_agent.py:build_remote_coordinator()` returns an ADK `RemoteA2aAgent` (`google.adk.agents.remote_a2a_agent`) client, and `try_build_remote_coordinator()` returns `None` on any failure. `src/registry.py` adds `register_a2a_agent(card)` / `get_a2a_agents()` that reuse the same `AgentRegistry` client as the MCP flow. `src/deploy/register_a2a.py` is the CLI (register default, `--discover` to list).

This is **preview-optional**: the A2A/registry create+discovery surface may not be enabled in every project. Every path degrades gracefully — it logs `A2A preview not enabled — skipping` and returns `None`/`[]` (the CLI exits 0) rather than crashing a live run. `a2a-sdk` 1.x models are protobuf, so serialization uses `protobuf.json_format.MessageToDict` (with a pydantic `model_dump` fallback).

### Model resolution

`src/config.py:resolve_model()` resolves each model id by family: **Gemini 2.x** (and `models/` ids) pass as plain strings (regional endpoints); **Gemini 3.x** use the **native ADK `Gemini` class** on the global endpoint (`client_kwargs={"vertexai": True, "location": "global", "project": …}`) — LiteLlm mangles Gemini-3 thought signatures into bogus `function_calls`, so Gemini-3 must not go through it; **Claude** (and any other non-gemini id, incl. an explicit `vertex_ai/` prefix as an opt-in escape hatch) is wrapped in `LiteLlm(vertex_location="global")`. See [docs/notes/gemini3-native-model-resolution.md](docs/notes/gemini3-native-model-resolution.md).

### Shared config

`src/config.py` is the single shared config for all agents (standalone, coordinator, router) and eval — `resolve_model()`, model defaults, engine IDs, and env-var names all live here. It's bundled into every Agent Runtime deployment via `extra_packages=["src"]` (`src/deploy/deploy_agents.py`), so the router imports it directly. (The router previously carried a self-contained `src/router/config.py` copy; that duplication was removed.)

### Memory Bank + Session wiring (deploy)

Cross-session recall only persists if the deployed engine is backed by managed services. `deploy_agents._build_app()` wraps memory-enabled agents (detected via `_wants_memory()` — any agent holding a `PreloadMemoryTool`, i.e. the coordinator) in `vertexai.agent_engines.AdkApp(agent=..., memory_service_builder=_memory_service_builder, session_service_builder=_session_service_builder)` and passes that AdkApp to `agent_engines.create/update`. Non-memory agents (router, single-tier) deploy as raw agents — the runtime auto-wraps them in a default AdkApp with no persistent services. Both builders return Vertex services scoped to `AGENT_ENGINE_ID` (`VertexAiMemoryBankService` / `VertexAiSessionService`). Two verifications, complementary: `uv run python -m src.eval.verify_memory --user-id <id>` reads a user's persisted facts back from the *store* (`agent_engines.retrieve_memories`, scoped `{app_name, user_id}`), while `uv run python -m src.eval.verify_cross_session_recall --user-id alice` proves genuine cross-session *recall* — it states a preference in session A, polls until Memory Bank generates facts, then opens a **brand-new session B** for the same user and checks the preference resurfaces via `PreloadMemoryTool` (not same-session context). Prints `RECALL: PASS/FAIL` and exits non-zero on FAIL; defaults to the pinned coordinator engine.

### Evaluation

- **Batch eval** (`src/eval/multi_agent_batch_eval.py`): offline eval using `AgentInfo` descriptors (no live MCP connections). 6 metrics: response quality, hallucination, safety, tool use, instruction following, response match.
- **Simulated eval** (`src/eval/simulated_eval.py`): multi-turn eval against a deployed agent using Vertex AI's user simulator.
- **Eval configs** (`src/eval/agent_eval_configs.py`): test cases per agent plus `build_agent_info()` which constructs `AgentInfo` for offline eval.
- **Vertex eval pipeline** (`src/pipelines/`): the full eval DAG (deploy → traffic → batch ‖ simulated ‖ complexity → monitor → report) as a KFP v2 Managed Pipeline, submitted manually via `src.pipelines.submit` (workflow: `.github/workflows/eval_vertex.yaml`). Replaced the old GitHub-Actions eval job graph. See [docs/notes/vertex-eval-pipeline.md](docs/notes/vertex-eval-pipeline.md).
- **Periodic-snapshot quality eval (two honest surfaces)** — the canonical source is the **offline-eval bridge**, because the native Vertex Online Evaluators are platform-blocked (the managed Agent Engine runtime strips prompt/response content from traces, so every cycle returns `INSUFFICIENT_DATA` — verified empirically, see `docs/notes/offline-eval-monitoring-bridge.md` and memory `online-eval-content-capture-blocked`). The coordinator (a task executor) and the 5-tier router (an economic optimizer) get **separate series** so they are never scored on the same axis:
  - **Coordinator quality → `custom.googleapis.com/agent_eval/*`** (1-5 rubric, alert `< 3.0`). `src/eval/publish_offline_eval.py:publish_offline_scores()` extracts the three monitored quality metrics (`helpfulness`, `tool_use_accuracy`, `policy_compliance`) from the batch eval — which legitimately scores the *deployed* engine via the Vertex Gen AI Evaluation Service — scales them 0-1 → 1-5, tags them `eval_mode=offline`, and delegates to `src/eval/publish_eval_metrics.py:publish_eval_metrics()` (names strictly from `quality_alerts.ALL_MONITORED_METRICS` — no drift). Two of the three (`policy_compliance` and `tool_use_accuracy`) are custom pointwise `LLMMetric`s that the `client.evals` SDK can't score (`400 Error parsing JSON`), so `_apply_standalone_judges()` overwrites them in the batch via standalone judges before publish — `policy_judge.py` and the delegation-aware `tool_use_judge.py` (`geap_tool_use`, which — unlike the delegation-blind SDK `TOOL_USE_QUALITY` — does not penalize the coordinator's `transfer_to_agent` routing; see [docs/notes/coordinator-tool-use-quality.md](docs/notes/coordinator-tool-use-quality.md)). Both publish paths (`publish_offline_eval --run` and `run_all_evals`) run them.
  - **Router efficiency → `custom.googleapis.com/agent_router/*`** (native units). `src/eval/publish_router_efficiency.py:publish_router_efficiency()` publishes `routing_accuracy_pct` (%, alert `< 80`), `cost_savings_pct` (% vs all-Opus baseline, alert `< 50`), and `classifier_latency_ms` (ms, alert `> 8000`) **verbatim, no scaling**, from the complexity accuracy + cost-efficiency evals. Cost savings was previously computed and discarded; it is now a first-class monitored series. Alert directions/families are parametrized in `quality_alerts.ROUTER_MONITORED_METRICS`.
  - `run_all_evals` publishes both surfaces as its Phase 6 step; each CLI (`publish_offline_eval`, `publish_router_efficiency`) can publish standalone (`--from-json`/`--latest`/`--run`/`--dry-run`). `src/eval/verify_monitors.py` reads both series as two blocks (`coordinator_quality` + `router_efficiency`, canonical source; optional guarded BigQuery export via `--source bigquery`). The dead native `onlineEvaluator` setup and its deprecation shim were removed.
- **CI/CD eval gate (advisory, opt-in)** — a demo PR quality gate that doesn't slow dev, in two tiers (see `docs/notes/ci-eval-gate.md`). **Tier 1** (`tests/test_eval_gate_safety.py`) is a deterministic, no-cloud corpus check (adversarial prompts must be refused by `input_guardrail_callback`, benign ones must pass) that runs inside the already-required `tests.yaml`. **Tier 2** (`.github/workflows/eval_gate.yaml`) reuses `multi_agent_batch_eval` (via the new `--limit`/`_select_cases`) to score the coordinator on rubrics; it is label-gated (`run-eval`) / `workflow_dispatch`, WIF-skip-guarded, writes a PASS/FAIL table to `$GITHUB_STEP_SUMMARY`, and is intentionally **not** a required check (advisory). Requires repo vars `WIF_PROVIDER`/`WIF_SERVICE_ACCOUNT`/`AGENT_ENGINE_ID` and a `run-eval` label. Honest limit: rubric scoring needs a *deployed* engine (no local-inference path), so the gate scores the shared `AGENT_ENGINE_ID` engine, **not the PR diff** — a regression alarm + capability demo, not a per-diff gate.

### DOE experiments & the coordinator model bake-off

`src/doe/` turns the single-config eval pipeline into an experiment engine: a declarative `Factor` registry (`factors.py`) → fractional-factorial `build_design()` → **one Vertex `PipelineJob` per design point** (`launch.py`, subprocess-per-point so each point's env bakes at import time) → `harvest.py` (one row per point) → `analyze.py` (main effects + cost-quality frontier → `report.md`). `run_doe.py` is the CLI (dry-run default; `--execute` opt-in because `engine_env` factors deploy a fresh engine per point). See [docs/notes/doe-framework.md](docs/notes/doe-framework.md).

The **coordinator model bake-off** compares two coordinator deployments that differ only by backbone — `gemini-3.6-flash` (baseline, coded `-1`) vs `claude-sonnet-5` (candidate, `+1`). It borrows the `model_backend` factor (`factors.py`, coordinator-only: moves just `COORDINATOR_MODEL`, sub-agents fixed) to define the two backbones in one place, but `run_bakeoff` **owns the deploy/teardown lifecycle directly** rather than running the DOE PipelineJob fan-out (whose ephemeral per-point engines get deleted in the pipeline's exit handler before pairwise/traffic can reach them, and whose manifest never records an `engine_id`). It deploys **two persistent engines**, one subprocess each via `src/doe/deploy_coordinator.py` (so `COORDINATOR_MODEL` bakes at import time; the child prints its resource on a `BAKEOFF_ENGINE:` marker line), records both `engine_id`s in the run **manifest** (not `.env`, which holds only one coordinator id), and **tears both down in a guaranteed `finally`** (`--keep-engines` opts out). Three evidence streams fuse into one verdict:
- **Offline rubrics per engine** — `run_bakeoff` scores each *deployed* engine with `multi_agent_batch_eval` (mapping versioned metric keys → base names via `harvest._metric_base`/`BATCH_METRICS`); cost is **measured**, not assumed — `collect_token_usage` reads real `usage_metadata` off `stream_query` (isolated in the bake-off, not shared `_sdk_patches.py`) and `src/eval/cost_model.py` prices it fairly (Gemini per-token, Claude GSU→USD — constants are directional, verify before quoting); no usage → honest `n/a`, not a fake `$0`.
- **Pairwise SxS** (`src/eval/pairwise_eval.py`) — `PairwiseMetric` autorater (`flip_enabled`, `sampling_count=4`) → win-rate, with a standalone `google.genai` `Choice: A|B|TIE` fallback if the managed template 400s. `--from-manifest` picks gemini=baseline, claude=candidate.
- **Per-model-labeled traffic** — `generate_traffic --label model=<id>` keeps the two deployments as separate Cloud Monitoring series; `verify_monitors --group-by model` and the dashboard's per-model breakdown tiles read them back split (default ungrouped behavior unchanged).

`src/doe/bakeoff_report.py` (pure assembly) renders offline means + delta, pairwise win-rate, online p50/p95/error, and per-request $ with a one-line verdict → `bakeoff_report.md`. `run_bakeoff` also records **one Vertex AI Experiments run per backbone** (`src/observability/experiments.py:log_run`, best-effort; params `backbone`/`role` + scalar metrics) into the `coordinator-bakeoff` experiment — kept strictly separate from the router's `router-efficiency` experiment. Dormant by default (`--experiment-name`, default `coordinator-bakeoff`; `''` disables). `src/doe/run_bakeoff.py` chains all phases (preflight → deploy 2 → offline+cost → pairwise → traffic → verify+report → teardown), **dry-run by default** (mirrors `run_doe`); every phase entrypoint is injectable for offline wiring tests. Caveats: dataset ~50 curated cases (incl. multi-step + adversarial) not ≥1000; Gemini-only judge; self-driven traffic split (no native endpoint A/B). See [docs/notes/coordinator-model-bakeoff.md](docs/notes/coordinator-model-bakeoff.md).

### GEPA optimization

`src/optimize/run_optimize.py` runs the GEPA prompt optimizer. It applies ADK patches before running (extra fields, None inference guards, None score guards) because upstream ADK has strict pydantic validation and doesn't handle MCP timeouts gracefully.

### Security layers

- **Model Armor** (`src/armor/config.py`): server-side screening via Model Armor templates + client-side guardrail (blocklist patterns, length limits). This is the single shared module — both the coordinator and the router import it (the router previously had a duplicate `src/router/armor.py`, now removed). The pure validator `input_guardrail_callback` (Content|None) stays side-effect-free for testability; `guardrail_with_telemetry` wraps it to emit a `guardrail.blocked` OTel span event + a `custom.googleapis.com/agent_armor/blocked` metric on each block (telemetry is fully guarded and never changes the guard's decision). The coordinator wires `guardrail_with_telemetry` as its `before_agent_callback` and passes `generate_content_config=get_armored_generate_config(COORDINATOR_MODEL)` for server-side armor; the router runs `input_guardrail_callback` inside its `complexity_router_callback`. Server-side Model Armor is **model-family-aware**: `get_armored_generate_config(model)` attaches the region-scoped templates only for a Gemini-2.x backbone (where they're honored natively); Gemini-3 runs on the global endpoint (templates `400 TEMPLATE_NOT_FOUND`) and Claude runs via LiteLlm, so armor is omitted for both and the client-side guardrail is the guaranteed layer (see [docs/notes/gemini3-native-model-resolution.md](docs/notes/gemini3-native-model-resolution.md)). A project-level Model Armor **floor setting** (inspect-only) with **Cloud Logging** on (`scripts/setup_model_armor_floor_settings.sh`), plus template `logSanitizeOperations` (`scripts/setup_model_armor.sh`), feed the console Security-tab Model Armor dashboard; two caveats — our custom Cloud Run MCP servers aren't "Google Cloud MCP Servers" (that floor setting is a no-op for them), and floor-setting/template screening covers the Gemini `generateContent` path so the dashboard is richest with a native Gemini backbone. See [docs/notes/model-armor-security-dashboard.md](docs/notes/model-armor-security-dashboard.md).

## Key Conventions

- All agents export `root_agent` at module level plus `agent = SimpleNamespace(root_agent=...)` for ADK CLI compatibility.
- Agent instructions that say "GEPA-optimized" were produced by the optimizer — edit these only through re-optimization, not manually.
- MCP servers use FastMCP with `streamable-http` transport.
- Tests validate agent configuration (tool count, sub-agent names, callback presence) without requiring live GCP or MCP connections.
- `.env` is the source of truth for deployed engine IDs — `deploy_agents.py` auto-updates it after each deploy.
- SDK-first demo notebooks live in [`notebooks/demo/`](notebooks/demo/) (platform + evaluation tours on this repo's modules); every live/billable cell is opt-in behind a `GEAP_RUN_*` flag, and the eval notebook uses the offline bridge (native Online Evaluators are platform-blocked). See [notebooks/demo/README.md](notebooks/demo/README.md).

## Code standards

Always refer to [CODE_STANDARDS.md](./CODE_STANDARDS.md) when writing code and
making environment changes. Follow it for all git, Python tooling, and testing
conventions.
