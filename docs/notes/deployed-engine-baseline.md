# The deployed-engine baseline: what "configured correctly" means

*2026-08-21. The optimal serving configuration for an Agent Engine in this repo,
the evidence behind each setting, and the verifier that enforces it.*

Every expensive failure on this project has been a **configuration** failure, not
a code failure. The code was right and deployed; the engine was serving on the
wrong container size, the wrong tier models, or with a capability silently off.

That class of bug is uniquely nasty because **the engine keeps working**. The
deploy succeeds. Health checks pass. `stream_query` returns 200. Nothing is in
the logs. The engine is just quietly wrong until someone measures quality or
reads a spec by hand — which, for the 4Gi OOM, took four separate investigations
across two months.

So the baseline is **executable**, not prose:

| | |
| --- | --- |
| `src/deploy/engine_baseline.py` | the rules, as data, each with a `why` |
| `src/deploy/verify_engine_config.py` | reads the live spec, diffs, exits non-zero |

```bash
uv run python -m src.deploy.verify_engine_config           # coordinator + router from .env
uv run python -m src.deploy.verify_engine_config --engine-id <ID> --role router
uv run python -m src.deploy.verify_engine_config --why     # rationale for every check
uv run python -m src.deploy.verify_engine_config --json    # for CI
```

The baseline imports `LITELLM_MEMORY`/`LITELLM_CPU` from `deploy_agents` rather
than restating them, so "what we deploy" and "what we verify" are the same
constants by construction. A verifier that can drift from the deployer is worse
than none — it would bless a config the deployer never produces.

## Severity has a precise meaning

- **critical** — the engine is serving wrong or dropping traffic. Fails CI.
- **advisory** — a posture, cost, or latency choice worth seeing. Never fails.

The split is load-bearing. An advisory that fails CI gets the whole check
suppressed within a week, and then the criticals go with it.

## The rules and what they cost to learn

### Shared — every engine

| check | severity | why |
| --- | --- | --- |
| `memory` = 16Gi | critical | The 4Gi platform default OOM-kills workers mid-call on **every** backbone. Measured on a Gemini-only coordinator: 22/147 empty (180 empty *attempts*) at 4Gi, 0/147 (0 attempts) at 16Gi. No traceback, no shutdown log — a SIGKILL emits neither. |
| `cpu` = 4 | critical | Paired; `resource_limits` sets both or neither. |
| `identity` = AGENT_IDENTITY | critical | The per-engine SPIFFE identity is what the Agent Registry grant is *made to*. Without it the engine authenticates as a shared SA and `roles/agentregistry.viewer` on the engine principal buys nothing. |
| `telemetry` = true | critical | Without it there is no span tree, so Cloud Trace and the console Observability tab are blank — and every trace-derived diagnosis in these notes becomes unreproducible. |
| `vertex_backend` | critical | Otherwise google-genai takes the Developer API path and needs an API key the engine doesn't have. |
| `mcp_registry_names` | critical | The registry resource names are the *primary* resolution path. The direct-URL fallback covers a registry **failure**, not absence. |
| `genai_enterprise_alias` | advisory | ADK 2.7.1 reads `GOOGLE_GENAI_USE_ENTERPRISE` first and falls back with a DeprecationWarning. Log hygiene, not correctness. |
| `min_instances` ≥ 1 | advisory | Scale-to-zero makes the first request after idle pay a cold start, which surfaces as a slow or error-shaped stream. **Honest caveat below.** |
| `own_engine_id` | advisory | Sessions and Memory Bank are safe regardless (the runtime injects `GOOGLE_CLOUD_AGENT_ENGINE_ID` and `_runtime_engine_id()` prefers it), but a baked `AGENT_ENGINE_ID` naming a different engine still feeds client-side values like `coordinator_a2a_url()` and makes logs read as though the wrong engine is serving. |

### Coordinator

| check | severity | why |
| --- | --- | --- |
| `memory_bank` | critical | Cross-session recall is the headline capability. With it off the agent still answers, so the loss is silent until a demo asks it to remember something. |
| `memory_preload_cache` | advisory | ADK's stock `PreloadMemoryTool` re-runs a blocking 3-5s retrieve before **every** internal LLM hop with the same query. The caching subclass collapses that to once per invocation with no cross-invocation staleness, and emits the only span showing whether it happened. |
| `server_side_armor` | advisory | Model Armor templates are region-scoped and honored only on a Gemini-2.x backbone. On Gemini-3 (global endpoint) they 400; on Claude they're never sent. Both are supported postures — but they should be a *decision*, and the baked `MODEL_ARMOR_*` env makes armor look active when it isn't. |

### Router

Both of these are silent regressions that nothing else catches.

| check | severity | why |
| --- | --- | --- |
| `tier_models_pinned` | critical | **The repo's nastiest deploy trap.** A plain `deploy_agents router --update` bakes `config.py`'s Gemini-3 defaults and regresses the tiers. The deploy succeeds and the engine serves — on models the router was never tuned for. The tier env overrides are mandatory on every router deploy, and until now nothing enforced that. |
| `classifier_non_thinking` | critical | A thinking classifier spends its budget on reasoning and returns empty text, so `classify_complexity` takes its low-score fallback for *every* prompt and the router sends **all** traffic to the lite tier. It still answers, so the collapse is invisible without inspecting routes. |

## The keep-warm caveat, stated plainly

`min_instances` is **advisory, not critical**, and the reason matters. The one
measurement that attributed empty streams to `min_instances=1` was taken *before*
the 4Gi OOM was found, on an engine that also had the OOM. It is confounded, and
the honest reading is that raising the floor probably helped by reducing how
often a fresh worker had to be built — not that scale-to-zero causes empties.

So the floor is justified as a **latency and demo-readiness** setting, and this
note does not claim more. `DEFAULT_MIN_INSTANCES = 1` is a floor against idle,
not a throughput setting.

## Create picks a floor; update preserves one

`deploy_agent` (create) substitutes `DEFAULT_MIN_INSTANCES` when the flag is
unset. `update_agent` does **not** — it passes `None`, which omits the key and
preserves whatever the engine has.

The asymmetry is deliberate: a create has nothing to preserve and the platform
default is scale-to-zero, while our served engines run 4 and silently downgrading
them to 1 on every routine `--update` would be a regression nobody asked for.

## What the verifier found on first run (2026-08-21)

Run against the two engines `.env` names, it immediately found the thing four
investigations had missed by hand:

| engine | finding |
| --- | --- |
| `3639…` coordinator (**the `.env` default**) | **2 critical** — no `resourceLimits`, i.e. still on 4Gi. Deployed 2026-08-18, before the fix, and never redeployed. This is the engine every eval, monitor, notebook and CI gate resolves to by default. |
| `6134…` router | clean; 1 advisory |
| `4380…` probe | clean; 1 advisory |

The coordinator was remediated in place the same day with a targeted
`resourceLimits` patch (`updateMask=spec.deploymentSpec.resourceLimits`, ~4 min,
no repackage, no recreate) and now passes.

**That is the argument for this module in one line:** the fix had already shipped
and merged, and the engine everything points at was still broken, because
shipping a fix and *running* it are different events and nothing connected them.

## Known remaining drift

On `3639…`, all advisory, all needing a **full redeploy** rather than a spec
patch (env vars are baked at package time):

- `ENABLE_MEMORY_PRELOAD_CACHE` unset — running the stock per-hop retrieve.
- `GOOGLE_GENAI_USE_ENTERPRISE` unset — deprecation warning only.
- `COORDINATOR_MODEL=gemini-3.5-flash` — server-side Model Armor inert; the
  client-side guardrail is the only screening layer. Note the probe `4380…` was
  deliberately moved to `gemini-2.5-flash` **for** server-side armor, so the two
  coordinators currently have different security postures. Worth reconciling
  intentionally rather than by accident.

Separately, `.env` still names five tier engine IDs (`LITE_/FLASH_/PRO_/SONNET_/
OPUS_ENGINE_ID`) that **no longer exist**. `cross_model_experiment.py`,
`generate_optimization_report.py` and `setup_apphub.sh` read them and will target
nothing. Left as-is pending a decision to redeploy the tier agents or drop the
vars.

## Scope

Read-only: a GET against the control plane. It never deploys, never mutates an
engine, never touches `.env`.

It checks the **spec**, not behaviour — the behavioural verifiers are separate
and complementary (`verify_mcp_tools`, `verify_cross_session_recall`,
`verify_router_health`, `verify_monitors`). A spec PASS means the engine is
configured the way we intend; it does not mean the engine is healthy.

Targets come from config rather than by listing the project, because the project
is **shared** with other solutions (58 engines; only 3 carry
`solution=geap-tour`). Listing would invite reporting on — or worse, acting on —
engines that are not ours.

Related: [empty-at-200-field-guide.md](./empty-at-200-field-guide.md),
[router-claude-tier-oom.md](./router-claude-tier-oom.md),
[agent-registry-mcp-resolution.md](./agent-registry-mcp-resolution.md),
[coordinator-latency-attribution.md](./coordinator-latency-attribution.md).
