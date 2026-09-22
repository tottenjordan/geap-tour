# SDK-first demo notebooks

Two narrative notebooks that tour the Gemini Enterprise Agent Platform using the
L1-native SDKs (`vertexai`, `google.adk`, `vertexai.agent_engines`,
`google.genai` evals) driven through **this repo's** modules — not a fork. Both
run top-to-bottom safely: every live/billable cell is opt-in behind a
`GEAP_RUN_*` env flag (default off).

| Notebook | Demonstrates |
| --- | --- |
| [`platform_sdk_demo.ipynb`](./platform_sdk_demo.ipynb) | MCP tool servers → build ADK agents → run/deploy on Agent Runtime → A2A register/discover → 5-tier complexity routing with **measured** per-model cost. |
| [`evaluation_sdk_demo.ipynb`](./evaluation_sdk_demo.ipynb) | Design metrics → score the deployed engine (`client.evals.*` / `multi_agent_batch_eval`) → publish two honest monitored surfaces via the **offline bridge** → regression / simulated / offline-over-BQ → failure clustering → GEPA optimizer. |

## Required environment

- A reachable **deployed** coordinator engine. Rubric/query cells score a live
  engine — there is no local-inference path (see memory
  `eval-requires-deployed-engine`). `.env` is the source of truth for
  `AGENT_ENGINE_ID`; it currently pins a known-good pre-rollout engine (see
  `coordinator-outage-is-runtime-not-model`).
- `uv sync` (project deps). Run cells with `uv run jupyter ...` or any kernel
  whose interpreter is the project venv.

## Opt-in flags (all default off/safe)

| Flag | Unlocks |
| --- | --- |
| `GEAP_RUN_QUERY=1` | Stream against the deployed engine; run the async complexity classifier. |
| `GEAP_RUN_DEPLOY=1` | `run_deploy(...)` — creates billable Agent Runtime engines (~3-5 min each). |
| `GEAP_RUN_MEMORY=1` | Cross-session Memory Bank recall — preference set in session A, recalled in a fresh session B (live, ~1-2 min). |
| `GEAP_RUN_EVAL=1` | Rapid rubric eval + publish to Cloud Monitoring + router-efficiency + failure clustering. |
| `GEAP_RUN_OPT=1` | Run the GEPA optimizer (heavy). |

## Honesty note

The evaluation notebook uses the **offline bridge** rather than the native Vertex
Online Evaluators. This section used to say the runtime "strips prompt/response
content from ADK traces" so the native path is permanently blocked — **that was
wrong, and was corrected on 2026-08-15.** The managed `AdkApp` runtime forces the
ADK span-content gate closed unless the engine is deployed with
`AdkApp(enable_tracing=True)`; that lever is ours, wired behind the opt-in
`ENABLE_SPAN_CONTENT_CAPTURE` flag, and validated live (46/46 `call_llm` spans
carried real content). The bridge is the canonical surface **by choice** —
model-neutral, and it needs no privacy-off content capture on the served engine
(see
[../../docs/notes/online-eval-content-capture.md](../../docs/notes/online-eval-content-capture.md),
[../../docs/notes/offline-eval-monitoring-bridge.md](../../docs/notes/offline-eval-monitoring-bridge.md)). The canonical source is the
**offline-eval bridge** publishing two separate series — coordinator quality
(`custom.googleapis.com/agent_eval/*`, 1-5) and router efficiency
(`custom.googleapis.com/agent_router/*`, native units).
