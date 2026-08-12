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

# Continuous online evaluation (native Online Monitors — single supported flow)
uv run python -m src.eval.setup_online_evaluators create      # create monitor over coordinator+router (optional trailing sample-rate %)
uv run python -m src.eval.setup_online_evaluators verify      # read native results + bridge scores → agent_eval/*
uv run python -m src.eval.verify_monitors --format json       # summarize agent_eval/* quality series (canonical source)

# Vertex Managed Pipeline (runs the full eval DAG on Vertex Pipelines)
bash scripts/build_eval_image.sh v1                                  # build+push runner image (one-time)
uv run python -m src.pipelines.submit --agent-id <ENGINE_ID> --skip-traffic   # reuse engine (fastest)
uv run python -m src.pipelines.submit --agent-module coordinator_agent        # full parity, fresh temp deploy

# Infrastructure setup (shell scripts)
bash scripts/deploy_all.sh              # full end-to-end
bash scripts/setup_governance_policies.sh --sgp   # IAM + SGP
```

## Architecture

### Two agent topologies

1. **Coordinator agent** (`src/agents/coordinator_agent.py`) — domain router with two sub-agents: `travel_agent` (flights/hotels via search + booking MCP) and `expense_agent` (policy checks/submissions via expense MCP). Uses `AgentTool` for delegation and `PreloadMemoryTool` for Memory Bank.

2. **Multi-model router** (`src/router/agents.py`) — 5-tier complexity router. A `before_agent_callback` calls the complexity classifier (`src/router/complexity.py`) which scores prompts 0–1, then routes to one of five sub-agents: lite (gemini-3.1-flash-lite) → flash (gemini-3.5-flash) → pro (gemini-3.1-pro-preview) → sonnet (claude-sonnet-4-6) → opus (claude-opus-4-6). The router itself runs on the lite model. Score boundaries (defaults, DOE-tuned for cost savings in screening doe-screening-20260812-073603): <0.44 lite, 0.44–0.60 flash, 0.60–0.80 sonnet, 0.80–0.95 pro, ≥0.95 opus. All four cut-points are env-overridable (`COMPLEXITY_LOW`/`MEDIUM_SPLIT`/`COMPLEXITY_HIGH`/`HIGH_SPLIT`).

### MCP server registration

Agents connect to MCP servers through two mechanisms in `src/registry.py`:
- **Agent Registry** (primary): `get_mcp_tools(server_name)` looks up the server by its registered resource name via `AgentRegistry.get_mcp_toolset()`.
- **Direct URL** (fallback): falls back to Cloud Run URLs from `MCP_SERVER_URLS` mapping in `src/config.py`.

The three required env vars `SEARCH_MCP_SERVER`, `BOOKING_MCP_SERVER`, `EXPENSE_MCP_SERVER` hold Agent Registry resource names — these are NOT optional and will crash on import if missing.

### Model resolution

`src/config.py:resolve_model()` handles the Gemini 2.x vs 3.x split: Gemini 2.x models pass as plain strings (regional endpoints), while Gemini 3.x and Claude models are wrapped in `LiteLlm(vertex_location="global")` because they require the global endpoint.

### Shared config

`src/config.py` is the single shared config for all agents (standalone, coordinator, router) and eval — `resolve_model()`, model defaults, engine IDs, and env-var names all live here. It's bundled into every Agent Runtime deployment via `extra_packages=["src"]` (`src/deploy/deploy_agents.py`), so the router imports it directly. (The router previously carried a self-contained `src/router/config.py` copy; that duplication was removed.)

### Evaluation

- **Batch eval** (`src/eval/multi_agent_batch_eval.py`): offline eval using `AgentInfo` descriptors (no live MCP connections). 6 metrics: response quality, hallucination, safety, tool use, instruction following, response match.
- **Simulated eval** (`src/eval/simulated_eval.py`): multi-turn eval against a deployed agent using Vertex AI's user simulator.
- **Eval configs** (`src/eval/agent_eval_configs.py`): test cases per agent plus `build_agent_info()` which constructs `AgentInfo` for offline eval.
- **Vertex eval pipeline** (`src/pipelines/`): the full eval DAG (deploy → traffic → batch ‖ simulated ‖ complexity → monitor → report) as a KFP v2 Managed Pipeline, submitted manually via `src.pipelines.submit` (workflow: `.github/workflows/eval_vertex.yaml`). Replaced the old GitHub-Actions eval job graph. See [docs/notes/vertex-eval-pipeline.md](docs/notes/vertex-eval-pipeline.md).
- **Continuous online eval** — ONE canonical flow on native Online Monitors: `src/eval/setup_online_evaluators.py` (`create`/`verify`/`list`/`delete`/`cleanup`) creates an onlineEvaluator over the deployed coordinator + router (configurable sample rate + metric set) whose scores land in the console and Cloud Logging. `src/eval/publish_eval_metrics.py:publish_eval_metrics()` bridges those scores onto `custom.googleapis.com/agent_eval/*` (names strictly from `quality_alerts.ALL_MONITORED_METRICS` — no drift) so the alert policies + dashboard chart quality alongside traffic. `src/eval/verify_monitors.py` reads that `agent_eval/*` series (canonical source; optional guarded BigQuery export via `--source bigquery`). `src/eval/setup_online_monitors.py` is a deprecated shim that delegates to `setup_online_evaluators create`.

### GEPA optimization

`src/optimize/run_optimize.py` runs the GEPA prompt optimizer. It applies ADK patches before running (extra fields, None inference guards, None score guards) because upstream ADK has strict pydantic validation and doesn't handle MCP timeouts gracefully.

### Security layers

- **Model Armor** (`src/armor/config.py`): server-side screening via Model Armor templates + client-side guardrail (blocklist patterns, length limits). This is the single shared module — both the coordinator and the router import it (the router previously had a duplicate `src/router/armor.py`, now removed). The pure validator `input_guardrail_callback` (Content|None) stays side-effect-free for testability; `guardrail_with_telemetry` wraps it to emit a `guardrail.blocked` OTel span event + a `custom.googleapis.com/agent_armor/blocked` metric on each block (telemetry is fully guarded and never changes the guard's decision). The coordinator wires `guardrail_with_telemetry` as its `before_agent_callback` and passes `generate_content_config=get_armored_generate_config()` for server-side armor; the router runs `input_guardrail_callback` inside its `complexity_router_callback`.

## Key Conventions

- All agents export `root_agent` at module level plus `agent = SimpleNamespace(root_agent=...)` for ADK CLI compatibility.
- Agent instructions that say "GEPA-optimized" were produced by the optimizer — edit these only through re-optimization, not manually.
- MCP servers use FastMCP with `streamable-http` transport.
- Tests validate agent configuration (tool count, sub-agent names, callback presence) without requiring live GCP or MCP connections.
- `.env` is the source of truth for deployed engine IDs — `deploy_agents.py` auto-updates it after each deploy.

## Code standards

Always refer to [CODE_STANDARDS.md](./CODE_STANDARDS.md) when writing code and
making environment changes. Follow it for all git, Python tooling, and testing
conventions.
