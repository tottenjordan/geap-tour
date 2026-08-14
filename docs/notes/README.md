# Session Notes

Durable notes that outlive a single working session — things not recoverable
from the repo itself (git history, CLAUDE.md, existing docs). One topic per
file; keep this index short (< 200 lines).

## Index

- [Dependency management & the internal registry gotcha](./dependency-management.md)
  — why `uv lock` here resolves from PyPI, and the inaccessible Artifact Foundry
  mirror.
- [Vertex Managed Pipeline for evals](./vertex-eval-pipeline.md) — running the eval
  DAG on Vertex Pipelines: setup, submit commands, and three KFP gotchas
  (env-injection, import-time config, exit-handler cleanup).
- [Offline-eval → monitoring bridge](./offline-eval-monitoring-bridge.md) — why the
  native Online Evaluators are platform-blocked (`INSUFFICIENT_DATA`) and how the
  offline bridge became the canonical source for two honest surfaces: coordinator
  quality (`agent_eval/*`, 1-5) via `publish_offline_eval` and router efficiency
  (`agent_router/*`, native units) via `publish_router_efficiency`.
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
