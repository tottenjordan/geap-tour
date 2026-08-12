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
  offline bridge (`publish_offline_eval`) became the canonical `agent_eval/*`
  quality source: metric provenance, 0-1→1-5 scaling, run commands, caveats.
- [DOE framework for scaling experimentation](./doe-framework.md) — factor
  registry → fractional-factorial design → one PipelineJob per point → harvest →
  main-effects report; factor channels, subprocess-per-point, cost caveat.
- [`router_boundaries` factor was inert (and the fix)](./doe-router-boundaries-inert.md)
  — why the first screening's routing/cost metrics were identical across all 9
  runs, and wiring the cost eval to the real 5-tier router so the factor moves.
- [DOE harvest `--wait` path hang (root cause & hardening)](./doe-harvest-wait-path.md)
  — a transient live-poll stall could hang unattended for the 2h timeout with no
  output; fixed via GCS ground-truth fall-through, a heartbeat, and a download
  timeout.
- [GEAP live-demo provisioning & runbook (hybrid-vertex)](./geap-demo-provisioning.md)
  — one-time provisioning checklist + run-of-show for the four demo money-shots
  (observability, trace debugging, continuous eval, governance blocking).
- [Coordinator `tool_use_quality`: root cause & eval-harness fix](./coordinator-tool-use-quality.md)
  — the low score was mostly ~50% empty turns (SDK concurrency + cold start, no
  retry-on-empty); plus the dynamic-rubric ceiling, a real prompt gap, and the
  nested-delegation runtime limitation behind the booking flatten.
