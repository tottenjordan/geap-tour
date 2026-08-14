# Gemini-3 native model resolution + model-family-aware Model Armor

**What changed (2026-08-14):** `src/config.py:resolve_model()` now returns the **native ADK `Gemini`
class** for Gemini-3.x backbones (on the global endpoint) instead of wrapping them in `LiteLlm`, and
server-side Model Armor is now attached only for the Gemini-2.x path. This adopts the two applicable fixes
from a downstream fork's `gemini-3.7-flash` migration and pins ADK to our tested `2.6.3`.

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

## Coordinator-outage hypothesis (untested until the live probe runs)

Our `coordinator-outage-is-runtime-not-model` memory concluded the empty-stream outage was a **platform
regression**, having ruled out backbone/tracing/deps. But two facts make LiteLlm-on-Gemini-3 an untested
alternative: (1) the crash signature localizes the death **exactly at the LiteLlm completion boundary**
("failing engines never emit `LiteLLM completion()` … die between memory and the first Vertex/gRPC call"),
and (2) **every fresh engine we tested was LiteLlm-wrapped** — we never isolated a native-Gemini deploy.
The fork built a *fresh* coordinator on `gemini-3.7-flash` after dropping LiteLlm. This does not prove
causation (our symptom is SIGKILL/empty-stream; theirs was hallucinated tool calls), but it is worth a
controlled test.

**Live probe (operator, opt-in — does NOT touch the pinned `.env` engine):** deploy a brand-new engine
under its own display name and stream-probe it:

```bash
COORDINATOR_MODEL=gemini-3.5-flash \
  uv run python -m src.doe.deploy_coordinator --display-name coordinator-native-gemini-probe
# capture the "BAKEOFF_ENGINE: projects/.../reasoningEngines/<ID>" line, then stream_query it
# (events > 0 → native un-blocks fresh deploys; 0 → platform regression confirmed independent of LiteLlm)
# tear down with agent_engines.delete(<resource>, force=True) — engines live in us-central1 (leak risk)
```

**Result:** _(not yet run — fill in after the probe; then update the outage memory accordingly)_.

Related: [[coordinator-outage-is-runtime-not-model]] (memory),
[model-armor-security-dashboard](./model-armor-security-dashboard.md),
[[bakeoff-engine-location-and-leak]] (memory).
