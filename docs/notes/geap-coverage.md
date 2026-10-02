# GEAP coverage — what this repo demonstrates, and what it does not

**2026-10-02.** A map of how much of the Gemini Enterprise Agent Platform this repo
exercises, built from the code (`src/`, `scripts/`, `.github/`), a live read of
`hybrid-vertex`, and the 09-11 → 09-17 live audit in
[geap-services-audit-2026-09.md](./geap-services-audit-2026-09.md).

**Caveat on the yardstick.** The capability list is the repo's own build / scale /
govern / optimize framing, not one checked against Google's product docs: the docs
lookup was unavailable when this was written. Re-check the list against the current
product docs before treating "not covered" as exhaustive.

Status vocabulary: **Deep** (implemented, verified live, guarded by tests),
**Yes** (implemented and in use), **Partial** (some of the surface, or opt-in and
off by default), **Blocked** (implemented or provisioned, stopped by the platform),
**None** (no implementation in `src/` or `scripts/`).

---

## Build

| capability | status | where | notes |
|---|---|---|---|
| ADK agents | Deep | `src/agents/`, `src/router/` | Direct-tools coordinator, 5-tier router (one agent swapping its model per tier), travel and expense agents evaluated separately. |
| MCP tools | Deep | `src/mcp_servers/{search,booking,expense}` | FastMCP on Cloud Run, `streamable-http`, stateless. Tool annotations reach the wire; list tools are bounded. |
| Multi-model | Deep | `src/config.py:resolve_model`, `src/models/` | Gemini 2.x as strings, Gemini 3 via native `Gemini` on the global endpoint, Claude via `LiteLlm`. The router serves 3 of its 5 tiers on this workload (pro and opus get nothing after boundary tuning). |
| A2A | Partial | `src/a2a/`, `src/deploy/register_a2a.py` | Agent card, `RemoteA2aAgent` client, registry registration. Preview: every path degrades to a logged skip. |
| Skill Registry | Blocked (discovery) | `src/skills/` | Publishing is live and idempotent. Runtime discovery reads a different store from the one publishing writes, so `ENABLE_SKILL_REGISTRY` stays off. See [skill-registry.md](./skill-registry.md). |
| Code Execution / Agent Engine Sandbox | None | — | Deliberately no `code_executor` in the skills. |
| Example Store | None | — | Mentioned only in [geap-demo-provisioning.md](./geap-demo-provisioning.md). |
| Grounding (RAG Engine, Vertex AI Search) | None | — | Policy limits live in the expense MCP mock DB, not a retrieval source. |
| Agent Garden / Agent Designer | None | — | |
| Live / streaming audio | None | — | |

## Scale

| capability | status | where | notes |
|---|---|---|---|
| Agent Runtime | Deep | `src/deploy/deploy_agents.py`, `engine_baseline.py`, `verify_engine_config.py` | In-place `--update`, 16Gi for every engine, `min_instances` on create, and an executable config baseline that fails on critical drift. |
| Sessions | Yes | `deploy_agents._session_service_builder` | Managed `VertexAiSessionService` on every engine. The traffic generator reuses sessions because session creation is the throughput ceiling. |
| Memory Bank | Deep | `src/agents/caching_preload_memory_tool.py`, `src/eval/verify_memory.py`, `verify_cross_session_recall.py`, `seed_demo_memories.py` | Memories are keyed by engine id, not agent name. Recall is graded by a judge, not a substring match. Optional per-invocation preload cache. |
| Observability | Deep | `src/observability/`, `src/eval/quality_alerts.py`, `verify_monitors.py` | Custom metric families, rolling-baseline anomaly detection, managed engine latency and error alerts, Gateway fail-open metrics. Trace content capture is opt-in. |
| BigQuery agent analytics | Partial | `ENABLE_AGENT_ANALYTICS` | Wired, off by default. See [agent-analytics-bigquery.md](./agent-analytics-bigquery.md). |

## Govern

| capability | status | where | notes |
|---|---|---|---|
| Agent Identity | Yes | every engine | Per-engine `AGENT_IDENTITY` with narrow grants (`agentregistry.viewer`, `modelarmor.user`). A recreate mints a new identity and needs fresh grants. |
| Agent Registry | Yes, thin | `src/registry.py` | Primary path for MCP resolution, with a logged fallback to direct URLs. The registry holds duplicate MCP servers and stale agents from earlier runs. |
| Agent Gateway | Blocked | `scripts/setup_agent_gateway.sh` | Both gateways and their IAP and Model Armor extensions are provisioned. Attaching a gateway to any engine fails with `13 INTERNAL`, by update, PATCH or create; likely enrollment. Nothing is in force. |
| Model Armor | Yes | `src/armor/` | ADK `ModelArmorPlugin` screens every backbone in-engine. Inline templates are off since 2026-09-28 because of an intermittent `TEMPLATE_NOT_FOUND` platform bug. Plus floor settings and a client guardrail. |
| IAP / Semantic Governance policies | Yes (per 09-16 audit) | `scripts/setup_governance_policies.sh` | CEL conditions on the three MCP servers, keyed on the tool annotations. Not re-verified on 2026-10-02. |
| Network and data controls (PSC, VPC-SC, CMEK) | None | — | |
| Threat detection (Agent Engine Threat Detection, SCC) | None | — | |

## Optimize

| capability | status | where | notes |
|---|---|---|---|
| Gen AI Evaluation service | Deep | `src/eval/` | Batch rubrics, custom judges (policy, tool use, tool faithfulness), a Gemini judge panel, judge-vs-human calibration, versioned datasets. Every rubric path needs a deployed engine. |
| Multi-turn evaluation | Yes (own loop) | `src/eval/multi_turn_sim.py` | The SDK drops the user simulator on the deployed-engine path, so `simulated_eval` is single-turn; this module owns the loop and is validated against injected defects. |
| Native online evaluators | Partial | `ENABLE_SPAN_CONTENT_CAPTURE` | Validated live, off by default for privacy. The client-side online monitor is the shipped surface. |
| GEPA prompt optimization | Yes | `src/optimize/` | Coordinator, router and tier agents. |
| Vertex AI Experiments | Yes | `src/observability/experiments.py` | Bake-off and router-efficiency runs. |
| Vertex AI Pipelines | Yes | `src/pipelines/`, `src/doe/` | KFP eval DAG and DOE fan-out. |
| Gemini Enterprise app publishing | None | — | No registration of an agent into the Gemini Enterprise app in `src/` or `scripts/`. |

---

## Live state on 2026-10-02

- Coordinator `3639…` and router `6134…` were last updated 2026-09-28 and carry the
  #172/#173 Model Armor fixes. `verify_monitors`: all four surfaces healthy, no
  anomalies, online `infra_empty_rate` 0.
- Demo probe `4380…` was last updated 2026-09-17, before #172, so it still sends
  inline Model Armor templates.

## Gaps worth closing, in order of fit for this demo

1. **Grounding** — the expense policy as a RAG Engine or Vertex AI Search corpus
   would demonstrate retrieval on a real domain question, and give the policy judge
   a ground-truth source.
2. **Example Store** — few-shot examples for the router's classifier or the
   coordinator's booking flow; measurable with the existing eval surfaces.
3. **Gemini Enterprise app publishing** — the end-user surface for the coordinator.
4. **Code Execution / Sandbox** — an expense-report calculation would be a natural
   use; it would change the tool surface, which feeds `tool_use_accuracy`.
5. **Agent Gateway enforcement** — not a code gap; needs the `13 INTERNAL` resolved
   on the platform side. The dry-run procedure is in the services audit.
