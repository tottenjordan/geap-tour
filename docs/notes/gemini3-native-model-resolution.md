# Gemini-3 native model resolution + model-family-aware Model Armor

**What changed (2026-08-14):** `src/config.py:resolve_model()` now returns the **native ADK `Gemini`
class** for Gemini-3.x backbones (on the global endpoint) instead of wrapping them in `LiteLlm`, and
server-side Model Armor is now attached only for the Gemini-2.x path. This adopts the two applicable fixes
from a downstream fork's `gemini-3.7-flash` migration and pins ADK to our tested version
(`2.6.3` at the time; now `2.7.1` — see [adk-2.7.1-dependency-refresh.md](./adk-2.7.1-dependency-refresh.md)).

## Why — the fork's migration findings, assessed against our code

The fork (`jswortz/geap-tour`, `docs/geap_smoke_test_gaps.md → "Update — gemini-3.7-flash migration"`)
recorded five fixes it needed to run the coordinator on `gemini-3.7-flash`. Verdict per finding against
*this* repo:

| Fork finding | Our state (verified) | Verdict |
|---|---|---|
| **#2** LiteLlm mangles Gemini-3 thought signatures into bogus `function_calls` → use native ADK `Gemini` | `resolve_model()` wrapped **all** Gemini-3 (coordinator, sub-agents, 4/5 router tiers) in `LiteLlm(vertex_location="global")`; zero native usage. ADK itself emits a `[GEMINI_VIA_LITELLM]` warning advising the native class. | **Adopted** |
| **#3** Model Armor has no global support → omit server-side armor for non-gemini-2.x | `get_armored_generate_config()` attached a region-scoped (`us-central1`) template unconditionally. | **Adopted (coupled to #2)** |
| **#5** pin `google-adk==2.5.0` | We used `>=2` floors → resolve to `2.6.3`; our own outage investigation ruled out dep drift. | **Adapted** → pinned `==2.6.3` (match `uv.lock`), NOT 2.5.0 (that would downgrade below tested) |
| **#4** prefer `GCP_PROJECT_ID` over ambient `GOOGLE_CLOUD_PROJECT` | `src/config.py:10` reads only `GCP_PROJECT_ID`; never reads `GOOGLE_CLOUD_PROJECT`. | **Already safe — no change** |
| **#1** Gemini-3 global-only endpoint | Already targeted via global; the native switch carries `location=global` in `client_kwargs`. | **Already handled by #2** |

## The #2 ↔ #3 coupling

Switching Gemini-3 to the **native** global path makes the region-scoped Model Armor template *actually
take effect* and then `400 TEMPLATE_NOT_FOUND`. Under LiteLlm the coordinator's own comment admitted the
armor field was "carried but honored only where the LiteLlm path forwards it" — i.e. effectively ignored.
So #3 ships with #2. The client-side guardrail (`armor.config.guardrail_with_telemetry`) is unchanged and
remains the **guaranteed** enforcement layer for every backbone; omitting server-side armor for Gemini-3
is honest (it never worked there), not a regression.

## Semantics preserved

`resolve_model()` is now a three-way branch:
- `gemini-2*` / `models/*` → plain string (regional endpoint).
- bare `gemini-3*` → `Gemini(model=…, client_kwargs={"vertexai": True, "location": "global", "project": GCP_PROJECT_ID})`.
- Claude and any other id, **including an explicit `vertex_ai/` prefix** → `LiteLlm(vertex_location="global")`.

The `vertex_ai/`-prefixed form is a deliberate opt-in escape hatch back to LiteLlm (kept for the
`test_already_prefixed_not_doubled` / bake-off-manifest cases). `get_armored_generate_config(model)`
mirrors the Gemini-2.x test via a local `_is_regional_gemini(model)` helper.

## Why ADK 2.6.3, not 2.5.0

The fork's exact pin exists to stop a local-pickle↔runtime ADK version skew (which can mis-load tools /
mangle model calls). Our deploy `REQUIREMENTS` and `pyproject.toml` used `>=2` floors that already resolve
to `2.6.3` (`uv.lock`). Pinning to `==2.6.3` gets the fork's determinism benefit **without** downgrading
below the version we test and lock against. Pinning to 2.5.0 as the fork did would be a downgrade and
contradicts our outage findings (see below), so it was rejected.

## Coordinator-outage hypothesis — tested 2026-08-14 (native-Gemini fresh deploy is healthy)

Our `coordinator-outage-is-runtime-not-model` memory concluded the empty-stream outage was a **platform
regression**, having ruled out backbone/tracing/deps. But two facts made LiteLlm-on-Gemini-3 an untested
alternative: (1) the crash signature localizes the death **exactly at the LiteLlm completion boundary**
("failing engines never emit `LiteLLM completion()` … die between memory and the first Vertex/gRPC call"),
and (2) **every fresh engine we tested was LiteLlm-wrapped** — we never isolated a native-Gemini deploy.
The fork built a *fresh* coordinator on `gemini-3.7-flash` after dropping LiteLlm. This did not prove
causation (our symptom is SIGKILL/empty-stream; theirs was hallucinated tool calls), so we ran a controlled
probe.

**Live probe (operator, opt-in — does NOT touch the pinned `.env` engine):** deploy a brand-new engine
under its own display name and stream-probe it with the honest event-counter `src.eval.probe_engine`:

```bash
COORDINATOR_MODEL=gemini-3.7-flash \
  uv run python -m src.doe.deploy_coordinator --display-name coordinator-native-gemini37-probe
# capture the "BAKEOFF_ENGINE: projects/.../reasoningEngines/<ID>" line, then:
uv run python -m src.eval.probe_engine <resource> --json    # ok=true, events>0 → it streams
# iterate the SAME engine as new revisions (in place; never writes .env):
COORDINATOR_MODEL=gemini-3.7-flash \
  uv run python -m src.doe.deploy_coordinator --update <ID> --display-name coordinator-native-gemini37-probe
# (engines live in us-central1 — teardown is agent_engines.delete(<resource>, force=True) if disposing)
```

### Observations (engine `…/reasoningEngines/4380288848559603712`, kept live)

| Measure | Result |
|---|---|
| Backbone / resolution | `gemini-3.7-flash` → **native ADK `Gemini`** (global endpoint), no LiteLlm |
| Engine | `coordinator-native-gemini37-probe`, **us-central1**, id `4380288848559603712` (kept running, not torn down) |
| Probe (multi-intent prompt) | `ok=true, events=1, text_events=0` — the single streamed event is a `function_call` (tool delegation, carries no visible text); first event 25.0s, elapsed 31.0s |
| Diagnostic — "What is 2+2?" | **1 event with visible text** ("2 + 2 is 4 …"); `finish_reason` + `usage_metadata` present |
| Diagnostic — "Find a flight SFO→JFK" | **3 events**: `function_call` → `function_response` (**real MCP flight results**) → text answer |
| Load (`--load --qps 2 --duration 1`) | Offered 119, **Sent OK 119, Errors 0**; achieved 0.40 qps (latency-bound, 8 workers); **p50 15.8s / p95 42.2s**; metrics emitted to `custom.googleapis.com/agent_traffic/*` labeled `model=gemini-3.7-flash` |
| Traces | Not fetched this run; per `online-eval-content-capture-blocked` the runtime strips prompt/response content — structure only |

**Result / interpretation (best read):** the fresh **native-Gemini** coordinator is **healthy** — it streams
events, returns visible answer text, executes real tool calls against the MCP servers, and served **0 errors
across 119 concurrent requests**. This is emphatically **not** the empty-stream outage (HTTP 200, 0 events,
SIGKILL) that crashed every fresh coordinator on 2026-08-13. So a fresh coordinator deploy **works again on
the native path as of 2026-08-14**.

**Honest confound — one arm, one day.** This single probe cannot fully separate two explanations: (a) the
**LiteLlm-on-Gemini-3 path** was the (or a) cause and dropping it un-blocks fresh deploys, vs. (b) the
**platform regression cleared** between 2026-08-13 and 2026-08-14 (in which case a fresh *LiteLlm* deploy
might now succeed too). We did **not** run the control arm (a fresh LiteLlm-wrapped Gemini-3 deploy today),
so we don't claim causation. What is established: **native Gemini-3 fresh coordinator deploys serve
healthily now**, and the native switch is the correct modernization regardless (it also kills the
`[GEMINI_VIA_LITELLM]` thought-signature mangling of finding #2). The `p50 15.8s` latency is high but
expected for a multi-tool coordinator doing real MCP round-trips; no baseline to call it a regression.

**Follow-up (separate operator decision, NOT done here):** repointing `.env` / redeploying the *real*
coordinator on the native path is gated on this result and left to the operator. The probe engine is
**kept live** and iterated as **in-place revisions** via `deploy_coordinator --update <ID>` (never rewrites
`.env`, never touches the pinned engine `3639024497392091136`).

## Genai completion-hook UPLOAD test on the native path — no-op + latency cost (2026-08-14)

The native switch made a second question testable. The genai completion-hook upload path
(`OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK=upload` + `OTEL_INSTRUMENTATION_GENAI_UPLOAD_{FORMAT,BASE_PATH}`)
fires only from native `google.genai` `generate_content` instrumentation, so the old "every agent is
LiteLlm, it can never fire" reasoning (PR #22's original Group-B rejection) no longer holds once the
coordinator runs native Gemini-3. We tested it **on the live probe engine** (`…/4380288848559603712`) by
baking the three upload vars into a revision (an uncommitted `_build_config` env passthrough — reverted
after) pointed at `gs://…/otel-genai-probe`, then A/B-probing clean vs hook:

| Measure | Clean | Hook |
| --- | --- | --- |
| Content uploaded to GCS | — | **ZERO** `_inputs/_outputs` JSONL over 55+ healthy streams |
| Empty-stream failure rate | 7.5% (3/40, two 20-probe blocks) | 10% (2/20) — **Fisher's exact p=1.0, no effect** |
| Median latency (successful streams) | **12.7s** | **18.9s** (~+6s / +50%) |

**Result:** the completion-hook upload is a **no-op even on the native path in the managed runtime** — the
runtime never invokes it — and it **adds ~6s median request latency** for no captured content. It does
**not** affect the empty-stream failure rate (that ~5-10% flakiness is background platform behavior:
even the two clean blocks swung 5%↔10%, wider than the clean-vs-hook gap). So PR #22 bakes it out
deliberately, guarded by `tests/test_deploy_agents.py::test_build_config_omits_genai_upload_hook`. Method:
`src/eval/probe_engine.py` (honest event-counter) driven N times per block; deploy revisions via
`deploy_coordinator --update`.

Side note — the ~5-10% empty-stream flakiness seen here is milder than, but the same signature as, the
2026-08-13 outage; the native path is **mostly** healthy now, not perfectly (contrast the earlier
119-req/0-error load run). Background platform state, not the hook.

Related: [[coordinator-outage-is-runtime-not-model]] (memory),
[model-armor-security-dashboard](./model-armor-security-dashboard.md),
[[bakeoff-engine-location-and-leak]] (memory).
