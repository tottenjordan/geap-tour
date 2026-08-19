# Session Notes

Durable notes that outlive a single working session — things not recoverable
from the repo itself (git history, CLAUDE.md, existing docs). One topic per
file; keep this index short (< 200 lines).

## Index

- [Dependency management & the internal registry gotcha](./dependency-management.md)
  — why `uv lock` here resolves from PyPI, and the inaccessible Artifact Foundry
  mirror.
- [Type-checking (ty) baseline](./type-checking-baseline.md) — why a clean
  `uv run ty check src/` still reports ~69 diagnostics (untyped Vertex/ADK SDK
  surface, intentional monkeypatches, optional imports), what was safely fixed,
  and the rule for triaging new ones.
- [Vertex Managed Pipeline for evals](./vertex-eval-pipeline.md) — running the eval
  DAG on Vertex Pipelines: setup, submit commands, and three KFP gotchas
  (env-injection, import-time config, exit-handler cleanup).
- [Offline-eval → monitoring bridge](./offline-eval-monitoring-bridge.md) — why the
  native Online Evaluators are platform-blocked (`INSUFFICIENT_DATA`) and how the
  offline bridge became the canonical source for two honest surfaces: coordinator
  quality (`agent_eval/*`, 1-5) via `publish_offline_eval` and router efficiency
  (`agent_router/*`, native units) via `publish_router_efficiency`.
- [Evaluation robustness — assessment + roadmap](./evaluation-robustness-roadmap.md)
  — grounded audit of the eval surface (G1–G6: judge non-determinism, train/eval
  contamination, no statistical rigor, static thresholds, coverage holes, correctness
  nits) with a P0/P1/P2 roadmap. **P0 shipped:** shared deterministic+retry judge
  client, a held-out eval split with a contamination guard test, and the CLI
  threshold-default fix.
- [Online quality monitor (`agent_online_eval/*`)](./online-quality-monitor.md) —
  continuous client-side eval: scores sampled live `stream_query` traffic with the
  same rubrics as the offline bridge and publishes a third monitored surface
  (`eval_mode=online`, same 1-5/3.0 axis) — sidesteps the platform-blocked native
  Online Evaluators by using client-side response content the trace surface strips.
- [Infra-empty separation + rolling-baseline alerts](./online-infra-empty-and-baseline-alerts.md)
  — (P2.8) empty-at-200 / error-shaped responses are partitioned out before judging
  and tracked as their own `infra_empty_rate` ceiling (GT) so empty streams stop
  masquerading as low quality; `verify_monitors` adds a rolling-baseline z-score
  anomaly block that catches drift the static floor misses.
- [Online-eval INSUFFICIENT_DATA — true root cause & fix](./online-eval-content-capture.md)
  — the native Online Evaluators failed because the managed runtime's `set_up()`
  forces the ADK span-content gate closed unless deployed with
  `AdkApp(enable_tracing=True)` (NOT a hard content strip); the opt-in
  `ENABLE_SPAN_CONTENT_CAPTURE` flag opens it, validated live (46/46 spans carry
  real content). Corrects the earlier "no lever" conclusion.
- [DOE framework for scaling experimentation](./doe-framework.md) — factor
  registry → fractional-factorial design → one PipelineJob per point → harvest →
  main-effects report; factor channels, subprocess-per-point, cost caveat.
- [Coordinator model bake-off: Gemini vs Claude](./coordinator-model-bakeoff.md)
  — single-factor (`model_backend`) DOE deploying two coordinators, scored on
  offline rubrics + pairwise SxS win-rate + per-model-labeled traffic, fused into
  one verdict by `run_bakeoff`; honest caveats (dataset ~50, Gemini-only judge,
  directional pricing, self-driven traffic split).
- [`router_boundaries` factor was inert (and the fix)](./doe-router-boundaries-inert.md)
  — why the first screening's routing/cost metrics were identical across all 9
  runs, and wiring the cost eval to the real 5-tier router so the factor moves.
- [Router end-to-end streaming: transfer → direct-tools](./router-transfer-streaming.md)
  — `transfer_to_agent`/`sub_agents` never streamed the specialist's turn on the
  managed runtime; rearchitected to one direct-tools agent that swaps its model per
  tier via a stateless `TierRoutingLlm` dispatcher. Residual empty-at-200 is
  platform-wide (coordinator empties at the same rate same-moment), not the router.
- [DOE harvest `--wait` path hang (root cause & hardening)](./doe-harvest-wait-path.md)
  — a transient live-poll stall could hang unattended for the 2h timeout with no
  output; fixed via GCS ground-truth fall-through, a heartbeat, and a download
  timeout.
- [GEAP live-demo provisioning & runbook (hybrid-vertex)](./geap-demo-provisioning.md)
  — one-time provisioning checklist + run-of-show for the four demo money-shots
  (observability, trace debugging, periodic-snapshot eval, governance blocking).
- [Agent-analytics content logging to BigQuery](./agent-analytics-bigquery.md) —
  opt-in `BigQueryAgentAnalyticsPlugin` (runner-level, model-neutral) streams full
  prompt/response/tool content to BQ independent of the OTEL surface the managed
  runtime strips; flags, IAM prereqs, and the pending live-capture gate.
- [Tool-call faithfulness (did it do what it said?)](./tool-call-faithfulness.md) —
  a grounded judge compares the agent's completion claims against the real executed
  `stream_query` trajectory to catch **hallucinated actions** (the gap
  `tool_use_judge` explicitly can't cover — `run_inference` yields text but no
  trajectory); publishes `agent_eval/tool_faithfulness` (offline) +
  `agent_online_eval/tool_faithfulness` (online), floor 3.0. Load-bearing Branch-A/B
  trajectory-visibility fork **resolved live 2026-08-18 → Branch A** (nested domain
  MCP calls visible client-side, so coordinator faithfulness is action-level).
- [Tool-call faithfulness — the console demo](./tool-faithfulness-demo.md) — a
  curated 5-case dataset (`src/eval/data/faithfulness_demo.json`) where look-alike
  confident responses differ only in the executed trajectory; the eval catches 3
  fabrications → 2.60/5, below the floor, moving the console tile + firing the alert.
- [Coordinator `tool_use_quality` ~0.27: root-cause finding](./coordinator-tool-use-quality.md)
  — mis-rubric (generic `TOOL_USE_QUALITY` wired instead of the delegation-aware
  `geap_tool_use`) plus a suspected trajectory-capture artifact; not an agent
  defect. Recommends a `policy_judge`-style standalone scorer; no fix shipped.
- [Model Armor Security dashboard](./model-armor-security-dashboard.md) — what feeds
  the console Security-tab Model Armor dashboard, the no-preview path we chose (floor
  settings inspect-only + Cloud Logging + template logging), and two honesty caveats
  (custom-MCP ≠ Google-MCP; LiteLlm/Claude coverage gap).
- [CI/CD eval gate (advisory, opt-in)](./ci-eval-gate.md) — a demo quality gate that
  doesn't slow dev: always-on deterministic safety checks (Tier 1) plus an opt-in,
  label-gated rubric eval (Tier 2) against the shared deployed engine; honest
  limitation that it scores the deployed engine, not the PR diff.
- [Agent Registry MCP resolution — two failure surfaces](./agent-registry-mcp-resolution.md)
  — the deployed coordinator fell back to direct Cloud Run URLs because it ran under
  a per-engine `AGENT_IDENTITY` that lacked `agentregistry.mcpServers.get` (a 403
  wrong-principal IAM denial surfaced by the loud fallback — NOT a platform block, and
  not fixed by granting the RE service agent). **Remediated (2026-08-15):** granted
  `roles/agentregistry.viewer` to the engine's `principal://<effectiveIdentity>` and
  recycled its cached toolsets with an in-place `--update` (toolsets resolve once per
  container; a *recreate* needs a fresh grant — see "Step 0b" in
  `setup_governance_policies.sh`). The fallback is now loud, the MCP servers are
  stateless (kills "Session terminated"), and `verify_mcp_tools` detects tool-less
  toolsets.
- [Agent Engine `stream_query` SSE-parse skew + raw-SSE fallback](./agent-engine-sse-stream-parse.md)
  — a recycled engine streams NDJSON via `:streamQuery?alt=sse`, but the installed
  (latest) `google-api-core` ships an **array-only** REST parser, so `stream_query`
  raises `Can only parse array of JSON objects` on a **healthy** engine. Fix:
  `src/eval/raw_stream.py`, a client-only raw-SSE reader yielding the same event
  dicts; the online monitor, faithfulness capture, `demo_readiness`, and steady
  traffic fall back to it on the skew. No redeploy; engine untouched.
- [Gemini-3 native model resolution + family-aware Model Armor](./gemini3-native-model-resolution.md)
  — why `resolve_model()` now returns native ADK `Gemini` for Gemini-3 (LiteLlm mangles
  thought signatures) and attaches server-side Model Armor only for Gemini-2.x; the
  fork's gemini-3.7 migration findings assessed, ADK pinned to 2.6.3, and an untested
  native-Gemini hypothesis for the coordinator outage.
- [Coordinator latency attribution + thinking-budget knob](./coordinator-latency-attribution.md)
  — `latency_probe.py` buckets the ~17s p50 by phase: MCP tools are cheap (0.2–1.1s),
  **startup/time-to-first-event dominates** (3.6–13.3s) and Memory Bank preload adds
  3–5s/turn. Ships opt-in `COORDINATOR_THINKING_BUDGET` / `COORDINATOR_MAX_OUTPUT_TOKENS`
  knobs (regional-Gemini path only, default unset = no change) + the live A/B to
  validate before changing the served default; memory-preload cache is a follow-up.
