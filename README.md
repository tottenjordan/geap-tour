# GEAP Workshop: Enterprise Agent Platform Tour

A hands-on workshop demonstrating the full Gemini Enterprise Agent Platform (GEAP) — from building ADK agents with MCP tools through deployment, governance, evaluation, and optimization.

## What's Inside

| Area | Description |
|------|-------------|
| **ADK Agents** | 7 `deploy_agents` targets — coordinator (direct MCP tools + Memory Bank), multi-model router, and 5 model-tier agents (lite, flash, pro, sonnet, opus) — plus travel and expense agents with their own evalsets |
| **MCP Servers** | Three FastMCP tool servers on Cloud Run with OTel instrumentation (search, booking, expense), stateless HTTP, bounded list payloads |
| **Multi-Model Router** | 5-tier complexity router across Gemini and Claude — one direct-tools agent that swaps **model + prompt** per tier |
| **Memory Bank** | Cross-session recall via `VertexAiMemoryBankService`, an optional per-invocation preload cache, and seed/verify CLIs |
| **Deployment** | Agent Runtime deployment with SPIFFE identity, gateway, keep-warm `--min-instances`, and OTel tracing |
| **Evaluation** | Offline batch (6 metrics), multi-turn simulated eval, a continuous **online quality monitor**, tool-call **faithfulness**, a diverse judge panel, and judge-vs-human calibration |
| **Monitoring** | Three published metric surfaces (`agent_eval/*`, `agent_online_eval/*`, `agent_router/*`) plus managed engine-health alerts and rolling-baseline anomaly detection |
| **Experiments** | DOE framework (fractional-factorial → one Vertex PipelineJob per design point) and a Gemini-vs-Claude coordinator bake-off |
| **Optimization** | GEPA (Gemini Evolutionary Prompt Algorithm) with sampler configs for the coordinator, router, travel, expense, and all 5 model-tier agents |
| **Model Armor** | Model Armor templates for input/output screening + a client-side guardrail with block telemetry |
| **Governance** | Agent identity (SPIFFE), agent gateway (ingress + egress), Agent Registry, Semantic Governance Policies (SGP) |
| **A2A** | Agent-to-agent card publication + discovery for the coordinator (preview-optional, degrades gracefully) |
| **Topology** | App Hub registration for agent-to-MCP topology visualization |
| **CI/CD** | Always-on unit tests, an advisory label-gated eval gate, and the eval DAG as a Vertex Managed Pipeline |

## Documentation

| Document | Description |
|----------|-------------|
| [Workshop Guide](docs/workshop_guide.md) | Full 4-session hands-on walkthrough |
| [Component FAQ](docs/faq.md) | What each component does and why it matters |
| [Evaluation Guide](docs/eval_operations.md) | Evaluation pipeline operations |
| [Engineering Notes](docs/notes/README.md) | 30 root-cause / design notes (streaming, quota, latency, memory scope, eval bridges) |
| [Demo Notebooks](notebooks/demo/README.md) | SDK-first platform + evaluation tours; every billable cell is opt-in |
| [GEPA Analysis](docs/gepa_optimization_analysis.md) | Prompt optimization before/after results |
| [Cross-Model Experiment](docs/cross_model_experiment.md) | All models × all complexity tiers |
| [Cost Comparison](docs/multi_model_cost_comparison.md) | Multi-model routing cost analysis |
| [Code Standards](CODE_STANDARDS.md) | Git, Python tooling, and testing conventions |
| [Slides](docs/slides.pptx) | Workshop deck (34 slides) |

## Quick Start

```bash
# Install dependencies
uv sync

# Copy and configure environment
cp .env.example .env
# Edit .env with your GCP project details

# Run tests (offline — no live GCP or MCP connections needed)
uv run pytest tests/

# Deploy everything in one command
bash scripts/deploy_all.sh

# Setup governance policies (IAM only)
bash scripts/setup_governance_policies.sh

# Setup governance policies with SGP (IAM + Semantic Governance Policies)
bash scripts/setup_governance_policies.sh --sgp
```

Common follow-ups (full command reference in [CLAUDE.md](CLAUDE.md)):

```bash
# Deploy or update a single agent (auto-writes the engine id to .env)
uv run python -m src.deploy.deploy_agents coordinator --update

# Prove the deployed agent's MCP toolsets actually resolve their tools
uv run python -m src.eval.verify_mcp_tools --json

# Score sampled live traffic and publish the online quality surface
uv run python -m src.eval.online_monitor --agent-id <ENGINE_ID>

# Read all three monitored surfaces back
uv run python -m src.eval.verify_monitors --format json
```

## Screenshots

All screenshots are captured from real deployed GCP resources:

| Screenshot | Feature |
|-----------|---------|
| ![Agent Gateway](docs/screenshots/session1_architecture_overview.png) | Agent Gateway ingress detail (geap-workshop-gateway) |
| ![Cloud Run](docs/screenshots/session1_cloud_run_mcp_detail.png) | MCP server on Cloud Run |
| ![Agent Engine](docs/screenshots/session1_agent_engine.png) | Multi-agent deployment |
| ![Agent Gateway](docs/screenshots/session2_agent_gateway.png) | Agent Gateway (ingress + egress) |
| ![Traces](docs/screenshots/session2_agent_traces.png) | Agent traces — session view with model calls and token usage |
| ![Trace Spans](docs/screenshots/session2_agent_trace_spans.png) | Trace spans — individual trace view |
| ![Model Armor](docs/screenshots/session4_model_armor.png) | Input/output screening |
| ![Evaluation](docs/screenshots/session2_evaluation_pipeline.png) | Three-tier eval pipeline |
| ![Agent Registry](docs/screenshots/session3_agent_registry_mcp.png) | MCP servers in Agent Registry |
| ![BigQuery Sink](docs/screenshots/session2_bigquery_sink.png) | Log Router sinks to BigQuery |
| ![Policies](docs/screenshots/session3_policies_iam.png) | IAM Allow governance policies |
| ![Business Policies](docs/screenshots/session3_business_policies.png) | Semantic Governance Policies (SGP) |

## Workshop Guide

See [docs/workshop_guide.md](docs/workshop_guide.md) for the full workshop organized into 4 sessions. For component-level details, see the [Component FAQ](docs/faq.md).

| Session | Topic | Duration |
|---------|-------|----------|
| **Session 1** | AI Gateway / MCP Gateway | ~90 min |
| **Session 2** | AI Gateway / MCP Gateway (continued) | ~75 min |
| **Session 3** | Agent Registry | ~15 min |
| **Session 4** | Model Security / Model Armor | ~15 min |

## Architecture

![GEAP Architecture](docs/screenshots/geap_architecture.png)

*Agent Platform architecture showing the full request flow: User → Frontend → Agent Gateway → Agent Identity (Agent Platform Runtime) → Agent Gateway → downstream Agents, Tools, Models, and APIs. Governed by Agent Registry, AI Security, and Access Authorization with full AI Observability.*

### Agent Identity Model

![Identities in Agentic Apps](docs/screenshots/identity_types.png)

The platform supports three identity types for secure agent operations:

| Identity | Purpose | Issuing System |
|----------|---------|----------------|
| **ID-1: User Identity** | User accessing the agent or SaaS application | Human IdP (Entra, Cloud Identity, Auth0) |
| **ID-2: Agent Identity** | Agent accessing resources under its own authority | GCP — created when agent is deployed |
| **ID-3: Delegated Identity** | Agent accessing resources on behalf of the user | OAuth server (1P or 3P) via OAuth dance |

In our workshop, agents use SPIFFE-based workload identity (ID-2) with attestation policies, and the Agent Gateway enforces identity at the network boundary.

### Two agent topologies

Both deployables hold their MCP toolsets **directly** on the root agent. That is not a style choice: on the managed Agent Runtime only the *root* agent's own output streams back, so delegating a turn (`transfer_to_agent`, or a nested `AgentTool` MCP call) never streamed the specialist's answer. Both agents were rearchitected around the pattern that does stream.

| | **Coordinator** (`src/agents/coordinator_agent.py`) | **Multi-model router** (`src/router/agents.py`) |
|---|---|---|
| Role | Task executor | Economic optimizer |
| Tools | All three MCP toolsets + a memory-preload tool | Same — all three MCP toolsets + a memory-preload tool |
| Per-request variation | None — one model, one prompt | Swaps **model and prompt** per complexity tier |
| Routing | Delegates to nobody | `before_agent_callback` scores 0–1, picks 1 of 5 tiers |
| Scored on | Quality rubrics (`agent_eval/*`, 1–5) | Efficiency (`agent_router/*`, native units) |

`travel_agent` and `expense_agent` are no longer wired under the coordinator, but they remain **independently evaluated** agents with their own evalsets (`multi_agent_batch_eval --agents travel_agent`) — separate deployables, not duplication.

Router tiers by complexity score (defaults, DOE-tuned for cost savings): `<0.44` lite (`gemini-3.1-flash-lite`), `0.44–0.60` flash (`gemini-3.5-flash`), `0.60–0.80` sonnet (`claude-sonnet-4-6`), `0.80–0.95` pro (`gemini-3.1-pro-preview`), `≥0.95` opus (`claude-opus-4-6`). All four cut-points are env-overridable (`COMPLEXITY_LOW` / `MEDIUM_SPLIT` / `COMPLEXITY_HIGH` / `HIGH_SPLIT`).

### Paper Banana Architecture Diagrams

> These were rendered before the direct-tools rearchitecture — diagram 01 still shows the coordinator delegating to travel/expense sub-agents, and 06 shows the eval gate as blocking rather than advisory. The tables above are the current source of truth.

| Diagram | Description |
|---------|-------------|
| ![Multi-Agent Topology](diagrams/outputs/01_multi_agent_topology.png) | Coordinator agent routing to travel and expense sub-agents with MCP tool servers |
| ![Deployment Architecture](diagrams/outputs/02_deployment_architecture.png) | Cloud Run MCP servers + Agent Runtime deployment topology |
| ![Evaluation Pipeline](diagrams/outputs/03_eval_pipeline.png) | Three-tier evaluation: one-time, continuous, and CI/CD simulated |
| ![Agent Identity & Gateway](diagrams/outputs/04_agent_identity_gateway.png) | SPIFFE identity, attestation policies, and Agent Gateway flow |
| ![Observability Stack](diagrams/outputs/05_observability_stack.png) | OTel traces → Cloud Trace → BigQuery pipeline |
| ![CI/CD Flow](diagrams/outputs/06_ci_cd_flow.png) | GitHub Actions simulated eval gate on pull requests |
| ![Model Armor](diagrams/outputs/07_agent_armor.png) | Model Armor input/output screening with guardrail callbacks |

## Project Structure

```
src/
├── agents/                    # Standalone deployable ADK agents
│   ├── coordinator_agent.py   # Direct-tools agent: all 3 MCP toolsets + Memory Bank
│   ├── caching_preload_memory_tool.py  # Opt-in per-invocation memory-preload cache
│   ├── travel_agent.py        # Flight/hotel search + booking
│   ├── expense_agent.py       # Expense submission + policy checks
│   ├── lite_agent.py          # Tier 1: gemini-3.1-flash-lite
│   ├── flash_agent.py         # Tier 2: gemini-3.5-flash
│   ├── pro_agent.py           # Tier 3: gemini-3.1-pro-preview
│   ├── sonnet_agent.py        # Tier 4: claude-sonnet-4-6
│   ├── opus_agent.py          # Tier 5: claude-opus-4-6
│   └── *_opt/, coordinator/   # GEPA optimization wrappers + evalsets
├── router/                    # Multi-model complexity router
│   ├── agents.py              # ONE direct-tools agent; swaps model + prompt per tier
│   ├── tier_routing_llm.py    # Stateless per-request model dispatcher
│   ├── complexity.py          # Prompt complexity classifier (0–1 score)
│   ├── cost_tracker.py        # Per-tier cost tracking
│   ├── demo.py, run_comparison.py  # Local demo + tier comparison
│   └── *_agent_opt/           # GEPA optimization wrappers
├── mcp_servers/               # FastMCP tool servers (Cloud Run, stateless HTTP)
│   ├── search/, booking/, expense/  # Flight+hotel search, booking, expenses
│   ├── auth.py                # MCP server auth
│   └── otel_setup.py          # Shared OTel instrumentation
├── eval/                      # Evaluation, judges, and monitoring publishers
│   ├── multi_agent_batch_eval.py    # Offline batch eval (6 metrics)
│   ├── simulated_eval.py            # Multi-turn simulated eval
│   ├── online_monitor.py            # Continuous client-side scoring of live traffic
│   ├── tool_faithfulness.py         # Did it really do what it claimed? (trajectory judge)
│   ├── policy_judge.py, tool_use_judge.py, judge_panel.py, judge_client.py
│   ├── calibration.py               # Judge-vs-human gold-set drift alarm
│   ├── publish_offline_eval.py      # Coordinator quality → agent_eval/*
│   ├── publish_router_efficiency.py # Router efficiency → agent_router/*
│   ├── quality_alerts.py, baseline.py, verify_monitors.py  # Alerts + z-score anomalies
│   ├── verify_mcp_tools.py, verify_memory.py, verify_cross_session_recall.py
│   ├── seed_demo_memories.py, latency_probe.py, cost_model.py, pairwise_eval.py
│   ├── complexity_metrics.py        # Router accuracy + cost efficiency
│   ├── cross_model_experiment.py    # All models × all tiers
│   ├── run_all_evals.py             # Full eval orchestration
│   ├── raw_stream.py                # Raw-SSE fallback for the stream_query parse skew
│   ├── agent_eval_configs.py        # Eval cases + AgentInfo descriptors
│   └── evalsets/, scenarios/, data/ # Test cases, simulator scenarios, curated sets
├── models/                    # Shared model plumbing
│   ├── quota_retry.py         # RetryingLlm — a 429 becomes slower, never empty
│   └── afc.py                 # with_afc_disabled — every GenerateContentConfig
├── armor/                     # Model Armor config + guardrail callbacks
├── observability/             # Tracing, custom metrics, dashboards, Vertex Experiments
├── doe/                       # Design-of-experiments + the coordinator model bake-off
│   ├── factors.py, design.py, launch.py, harvest.py, analyze.py
│   └── run_doe.py, run_bakeoff.py, bakeoff_report.py, deploy_coordinator.py
├── pipelines/                 # Eval + optimize DAGs as KFP Managed Pipelines
├── a2a/                       # Agent card build + RemoteA2aAgent client
├── deploy/                    # Deployment (Agent Runtime + Cloud Run)
│   ├── deploy_agents.py       # Deploy/update agents with auto .env write
│   ├── deploy_mcp_servers.py  # Deploy MCP servers to Cloud Run
│   ├── register_a2a.py        # Publish / discover the A2A agent card
│   └── deploy_all.py          # Python end-to-end deployment
├── optimize/                  # GEPA optimization configs + runner
│   └── run_optimize.py        # Python-native GEPA runner
├── traffic/                   # Traffic generation for OTel traces
│   └── generate_traffic.py    # Burst + steady-state traffic, pooled sessions
├── registry.py                # Agent Registry / MCP toolset + A2A discovery
└── config.py                  # Shared config, resolve_model(), model defaults

scripts/                       # Shell scripts for infrastructure setup
├── setup_apphub.sh            # App Hub topology registration
├── setup_agent_gateway.sh     # Agent Gateway (ingress + egress)
├── setup_agent_identity.sh    # SPIFFE agent identity
├── setup_governance_policies.sh     # IAM + SGP governance
├── setup_model_armor.sh, setup_model_armor_floor_settings.sh
├── setup_logging_sink.sh      # Log Router sink to BigQuery
├── build_eval_image.sh        # Build the Vertex pipeline runner image
└── deploy_all.sh              # Full end-to-end deployment

.github/workflows/
├── tests.yaml                 # Always-on unit tests (includes the Tier-1 safety corpus)
├── eval_gate.yaml             # Advisory, label-gated rubric eval on PRs
└── eval_vertex.yaml           # Submit the eval DAG to Vertex Pipelines

notebooks/demo/                # SDK-first platform + evaluation tours (opt-in billable cells)
diagrams/                      # Paper Banana architecture diagrams (inputs + outputs)
docs/                          # Workshop guide, analysis reports, charts, and notes
├── workshop_guide.md          # Full 4-session walkthrough
├── faq.md, eval_operations.md     # Component FAQ + eval operations
├── gepa_optimization_analysis.md  # GEPA before/after analysis
├── cross_model_experiment.md      # Cross-model complexity experiment
├── prompts/                       # Before/after prompt comparisons
├── charts/                        # Matplotlib + PaperBanana visualizations
└── notes/                         # Engineering session notes (indexed in notes/README.md)
tests/                         # 78 test files, offline — no live GCP or MCP needed
```
