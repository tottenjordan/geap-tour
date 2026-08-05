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

# Infrastructure setup (shell scripts)
bash scripts/deploy_all.sh              # full end-to-end
bash scripts/setup_governance_policies.sh --sgp   # IAM + SGP
```

## Architecture

### Two agent topologies

1. **Coordinator agent** (`src/agents/coordinator_agent.py`) — domain router with two sub-agents: `travel_agent` (flights/hotels via search + booking MCP) and `expense_agent` (policy checks/submissions via expense MCP). Uses `AgentTool` for delegation and `PreloadMemoryTool` for Memory Bank.

2. **Multi-model router** (`src/router/agents.py`) — 5-tier complexity router. A `before_agent_callback` calls the complexity classifier (`src/router/complexity.py`) which scores prompts 0–1, then routes to one of five sub-agents: lite (gemini-3.1-flash-lite) → flash (gemini-3.5-flash) → pro (gemini-3.1-pro-preview) → sonnet (claude-sonnet-4-6) → opus (claude-opus-4-6). The router itself runs on the lite model. Score boundaries: <0.30 lite, 0.30–0.45 flash, 0.45–0.60 sonnet, 0.60–0.80 pro, ≥0.80 opus.

### MCP server registration

Agents connect to MCP servers through two mechanisms in `src/registry.py`:
- **Agent Registry** (primary): `get_mcp_tools(server_name)` looks up the server by its registered resource name via `AgentRegistry.get_mcp_toolset()`.
- **Direct URL** (fallback): falls back to Cloud Run URLs from `MCP_SERVER_URLS` mapping in `src/config.py`.

The three required env vars `SEARCH_MCP_SERVER`, `BOOKING_MCP_SERVER`, `EXPENSE_MCP_SERVER` hold Agent Registry resource names — these are NOT optional and will crash on import if missing.

### Model resolution

`src/config.py:resolve_model()` handles the Gemini 2.x vs 3.x split: Gemini 2.x models pass as plain strings (regional endpoints), while Gemini 3.x and Claude models are wrapped in `LiteLlm(vertex_location="global")` because they require the global endpoint.

### Dual config files

`src/config.py` is the shared config used by standalone agents and eval. `src/router/config.py` is a self-contained copy for the router package — needed because Agent Runtime deploys each agent as an isolated package. When changing model defaults or env var names, update both files.

### Evaluation

- **Batch eval** (`src/eval/multi_agent_batch_eval.py`): offline eval using `AgentInfo` descriptors (no live MCP connections). 6 metrics: response quality, hallucination, safety, tool use, instruction following, response match.
- **Simulated eval** (`src/eval/simulated_eval.py`): multi-turn eval against a deployed agent using Vertex AI's user simulator. Used in CI (`eval_ci.yaml`).
- **Eval configs** (`src/eval/agent_eval_configs.py`): test cases per agent plus `build_agent_info()` which constructs `AgentInfo` for offline eval.

### GEPA optimization

`src/optimize/run_optimize.py` runs the GEPA prompt optimizer. It applies ADK patches before running (extra fields, None inference guards, None score guards) because upstream ADK has strict pydantic validation and doesn't handle MCP timeouts gracefully.

### Security layers

- **Model Armor** (`src/armor/config.py`, `src/router/armor.py`): server-side screening via Model Armor templates + client-side `input_guardrail_callback` (blocklist patterns, length limits). The armor config is duplicated between `src/armor/` (for coordinator) and `src/router/` (for router) — same reason as dual config.

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
