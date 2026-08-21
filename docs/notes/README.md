# Session Notes

Durable notes that outlive a single working session — things not recoverable
from the repo itself (git history, CLAUDE.md, existing docs). One topic per
file; keep this index short (< 200 lines).

## Index

- [Dependency management & the internal registry gotcha](./dependency-management.md)
  — why `uv lock` here resolves from PyPI, and the inaccessible Artifact Foundry mirror.
- [Type-checking (ty) baseline](./type-checking-baseline.md) — why a clean
  `uv run ty check src/` still reports diagnostics (untyped Vertex/ADK surface,
  intentional monkeypatches, optional imports) and the rule for triaging new ones.
- [Vertex Managed Pipeline for evals](./vertex-eval-pipeline.md) — running the eval
  DAG on Vertex Pipelines: setup, submit commands, and three KFP gotchas.
- [Offline-eval → monitoring bridge](./offline-eval-monitoring-bridge.md) — why the
  native Online Evaluators are platform-blocked (`INSUFFICIENT_DATA`) and how the
  offline bridge became the canonical source for two honest surfaces: coordinator
  quality (`agent_eval/*`, 1-5) via `publish_offline_eval` and router efficiency
  (`agent_router/*`, native units) via `publish_router_efficiency`.
- [Evaluation robustness — assessment + roadmap](./evaluation-robustness-roadmap.md)
  — grounded audit of the eval surface (G1–G6) with a P0/P1/P2 roadmap. **P0
  shipped:** deterministic+retry judge client, a held-out split with a contamination
  guard, and the CLI threshold-default fix.
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
  tier via a stateless `TierRoutingLlm` dispatcher. (Its "residual empties are
  platform-wide" claim is falsified — see the note below.)
- [Empty-at-200: which one is it?](./empty-at-200-field-guide.md) — **start here** for
  any zero-character HTTP 200. Five causes and the signature separating each. Cause 5
  is now resolved: the 4Gi platform default OOM-kills **Gemini-only** engines too, not
  just LiteLlm ones — 15% empty → **0%** at 16Gi, after concurrency, recycling, the
  SDK parser, sessions, Model Armor and the preload cache were each measured and
  refuted. If no client-side lever moves an empty rate, check `resourceLimits` first.
- [Router empty responses: an oversized tool payload burning the quota](./router-empty-responses-quota.md)
  — the router's ~40% empty-at-200 rate was HTTP 429 `RESOURCE_EXHAUSTED`, driven
  by an unbounded `get_expenses` payload (96 records/26KB) that a direct-tools
  agent must absorb and re-emit; fixed by capping the tool payload and retrying +
  labelling 429s instead of returning silence. Includes the falsified hypotheses.
- [The residual empty-at-200: wrapping LiteLlm strips Anthropic's tool-call ids](./router-empty-stream-retry.md)
  — the leftover 14% was **not** platform-level: `TierRoutingLlm`/`RetryingLlm` hide
  `LiteLlm` from ADK's `isinstance` check, so ADK strips the `adk-` tool-call ids
  Anthropic pairs results by and a multi-step, mixed-tier Claude turn dies on
  `AnthropicError: 'tool_call_id'`. Fixed by `restore_tool_call_ids()`.
- [The Claude tiers were OOM-killed: 4Gi is not enough for a LiteLlm engine](./router-claude-tier-oom.md)
  — a trace missing its enclosing `invoke_agent` span, no traceback, and a fresh
  worker booting 5.6s into the LiteLLM call = a SIGKILL: litellm (+140MB resident)
  overruns the Agent Runtime default. 16Gi took the probes 8/8 empty to **0/8**;
  `_auto_memory()` now derives the limit so it cannot silently revert.
- [Porting the router's fixes to the coordinator](./coordinator-router-learnings.md)
  — a two-engine trace census showed the gap was published *attributes*, not
  instrumentation; ports the payload cap (booking), a shared `RetryingLlm` 429
  wrapper (insurance — the coordinator took 0 of the 215 429s), domain spans for
  the un-traced 3-5s Memory Bank preload + the silently-swallowed save, and drops
  both AgentTools (0 calls measured across 10 traces).
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
- [Prompt audit: do our prompts describe the system we ship?](./prompt-architecture-audit.md)
  — `geap_tool_use`, which overwrites the monitored `tool_use_accuracy`, still
  described a delegating multi-agent system and spent 1 of 4 criteria on
  impossible delegation; the declared inventory was 7 of the real 10. Also fixed
  two GEPA prompts (owner decision), chiefly `expense_agent` refusing a submission
  its own eval case requires. All pinned by guard tests.
- [ADK eval metrics: what we use and what we don't](./adk-eval-metric-coverage.md)
  — the 2.6.3 → 2.7.1 upgrade added **no** eval metrics (`PrebuiltMetrics` is
  byte-identical), so the question is coverage: we used 6 of 13. Adopts
  `per_turn_user_simulator_quality_v1` (grades the *simulated user*) and
  `rubric_based_multi_turn_trajectory_quality_v1` (grades the multi-turn *path*).
- [Scoring the tool trajectory](./trajectory-criterion.md) — wires up
  `run_trajectory_eval`, which was finished, tested and called by nothing because it
  was pinned at zero **three** different ways: registry-prefixed names vs bare
  references (0% raw → 68% normalized), `EvalTask`'s concurrent fan-out plus an API
  that *rejects* an empty `predicted_trajectory` (`failure/mean 1.0`, all `nan`), and
  args compared against names-only references. Now 1.0/1.0/1.0 with empties
  partitioned out. The headline: ordering is **100% correct on every turn that calls
  a tool** — the rest is the infra-empty problem. Optimizer criterion deliberately
  deferred, with the unblocking experiment named.
- [GEPA sampler cases: what the optimizer was being taught](./gepa-sampler-case-audit.md)
  — the data behind that prompt defect. Swept all 13 evalsets: one case
  (`expense_over_limit_no_submit`) taught refuse-to-submit against a server that
  always records over-limit expenses as `pending_review`; 11 expected
  `submit_expense` with no policy check first; one expected a `search_flights` its
  prompt never asks for.
- [The "hallucination drift" was the judge being told the agent has no tools](./offline-eval-empty-turns.md)
  — `agent_data.agents` was `None`, so the judge graded real `function_call`s as
  contradictory; supplying a name-aligned `AgentInfo` took `tool_use_quality`
  0.38 → **0.93** and decoupled hallucination from the empty rate. Also: answer-less
  turns are now retried and every run reports its empty rate. Corrects two wrong
  guesses (aiplatform judge drift; PR #66's `AgentConfig.tools`).
  A 441-item sweep then showed the residual ~14% empty rate is **flat across
  concurrency 1/4/8** — a steady-state engine defect, not contention.
- [Router `tool_use_quality_v1`: "no function_call events found"](./router-tool-use-quality.md)
  — the metric grades the `AgentData` **events**, not the response text, so a run
  where nothing calls a tool is unscorable and came back silently reporting five
  metrics instead of six. Ships a `Tool calls: N/M` preflight, a router descriptor
  matching the direct-tools agent, and per-agent engine resolution.
- [Model Armor Security dashboard](./model-armor-security-dashboard.md) — what feeds
  the console Security-tab Model Armor dashboard, the no-preview path we chose (floor
  settings inspect-only + Cloud Logging + template logging), and two honesty caveats
  (custom-MCP ≠ Google-MCP; LiteLlm/Claude coverage gap).
- [CI/CD eval gate (advisory, opt-in)](./ci-eval-gate.md) — a demo quality gate that
  doesn't slow dev: always-on deterministic safety checks (Tier 1) plus an opt-in,
  label-gated rubric eval (Tier 2) against the shared deployed engine; honest
  limitation that it scores the deployed engine, not the PR diff.
- [Agent Registry MCP resolution — two failure surfaces](./agent-registry-mcp-resolution.md)
  — the coordinator fell back to direct Cloud Run URLs because its per-engine
  `AGENT_IDENTITY` lacked `agentregistry.mcpServers.get` (a 403 wrong-principal
  denial, not a platform block). Remediated by granting `roles/agentregistry.viewer`
  to the engine's own principal and recycling cached toolsets with an in-place
  `--update`; "Session terminated" fixed separately by `stateless_http`.
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
- [ADK 2.6.3 → 2.7.1 + dependency refresh](./adk-2.7.1-dependency-refresh.md) — the
  upgrade and its one silent hazard: 2.7.0 moved `PreloadMemoryTool`'s render to
  `_insert_transient_user_content` while leaving **both** methods on `LlmRequest`,
  so our caching subclass degraded silently (now a differential test). Plus the
  `GOOGLE_GENAI_USE_ENTERPRISE` rename, a dropped `google-cloud-trace`, the 11/11
  private-API audit, and the `--all-groups` test-collection trap.
- [`vertexai.Client` → `agentplatform.Client`](./agentplatform-client-migration.md)
  — not a rename: `agentplatform._genai` is a **separate copy**, so `Client`,
  `types` and the `_sdk_patches` targets must move as one unit or the patches
  silently no-op back to ~0 rubric scores. No dependency change; `vertexai.init` /
  `agent_engines` left alone. Engine logs stay noisy — those frames are ADK's.
- [The google-genai AFC warning](./genai-afc-warning.md) — google-genai defaults
  automatic function calling **on**, so every call took the AFC branch and the
  router logged an `AFC is enabled` INFO per request plus a WARNING per worker
  process (1000+ rows in 6h). Two emitters (our classifier + ADK's own call, which
  copies the agent's `generate_content_config` verbatim), one shared
  `src/models/afc.py:with_afc_disabled` stamp on every config we build. Invisible
  locally: the string only exists in google-genai ≥ 2.18.1.
