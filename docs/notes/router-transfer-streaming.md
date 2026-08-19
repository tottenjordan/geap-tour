# Router end-to-end streaming: transfer_to_agent → direct-tools dispatcher

**TL;DR** The 5-tier router used to be a *transferring root* over five
`sub_agents` (lite→opus). On the deployed Agent Engine runtime that never streamed
the transferred specialist's turn back to the client (~0/8 full completions) — the
same wall the coordinator hit with nested `AgentTool` MCP calls. The router was
rearchitected to **one** root agent that holds the MCP toolsets **directly** and,
per complexity tier, swaps **both** its *model* (via a stateless `TierRoutingLlm`
dispatcher) **and** its *instruction* (via an `InstructionProvider`) — so it keeps
full per-tier specialization without delegating. It now streams its own tool calls
+ synthesis end-to-end. Residual empty-at-200s are a **platform-wide** artifact
(the coordinator empties at the same rate at the same moment), not the router
architecture.

## Root cause: only the root agent's output streams

On the managed runtime, only the **root** agent's own output streams back
reliably. Delegation does not:

- `transfer_to_agent` (via `sub_agents`) — the specialist's post-transfer turn
  frequently never streams (empty-at-200).
- nested `AgentTool` whose sub-agent makes the MCP calls — same failure; this is
  precisely why `src/agents/coordinator_agent.py` holds its SEARCH/BOOKING/EXPENSE
  toolsets **directly** on the root and uses `AgentTool(travel_agent)` /
  `AgentTool(expense_agent)` only for conversational hand-offs, not for tool work.

The old router violated this: the root emitted `transfer_to_agent` and the chosen
tier sub-agent (which held the MCP tools) ran the actual turn — whose output did
not stream.

### Why the earlier "router-specific" reads were confounded

Two separate confounds made this look like a model/classifier bug at first:

1. **Different backbones.** An early "router 0/6 vs coordinator 3/3" compared a
   Gemini-3 router against a Gemini-2.5 coordinator. Re-testing with Gemini-2.5
   tiers on the router still showed the transfer failure — isolating *transfer*,
   not the model.
2. **Different moments.** Comparing a router run against a coordinator run taken
   ~40 min earlier. A **same-moment** control (identical spaced prompts, back to
   back) showed the coordinator empties at the **same rate** as the router — see
   below.

## The fix: one direct-tools agent + per-request model AND prompt dispatch

`src/router/agents.py` — `router_agent` is a single `LlmAgent`:

- **tools:** the three MCP toolsets held DIRECTLY + a memory tool (so its tool
  calls stream as top-level events the runtime forwards).
- **model:** `TierRoutingLlm(TIER_MODELS, default_model=LITE_MODEL)` — see
  `src/router/tier_routing_llm.py`. A stateless `BaseLlm` set once as
  `agent.model`. Mutating a shared `agent.model` per request would race across
  concurrent invocations, so instead the dispatcher, per request, reads the chosen
  model id from `llm_request.model` and forwards `generate_content_async` to that
  tier's pre-resolved backbone. No per-request state ⇒ race-safe.
- **instruction:** `tier_instruction_provider` — an ADK `InstructionProvider`
  (`Callable[[ReadonlyContext], str]`) rather than a static string. ADK resolves it
  while building each LLM request, *after* `complexity_router_callback` has stored
  `state["model_tier"]`, and returns `_TIER_TO_INSTRUCTION[tier]` — the SAME
  (GEPA-optimized) prompt the matching standalone tier agent carries — falling back
  to the generic `ROUTER_INSTRUCTION` when no tier is set. This mirrors the model
  dispatch so prompt and backbone always match the classifier's tier, restoring the
  per-tier prompt specialization the old five sub-agents provided. (Safe as a
  provider: the tier instructions contain no `{state}` template placeholders, and
  ADK sets `bypass_state_injection=True` for provider-sourced instructions.)
- **before_agent_callback:** `complexity_router_callback` — classifies the prompt,
  runs the input guardrail, records the routing span, stores `state["model_tier"]`.
- **before_model_callback:** `select_tier_model_callback` — writes
  `tier_to_model(state["model_tier"])` onto `llm_request.model` before every LLM
  hop, so a multi-hop turn stays on the classifier's tier.
- **no `sub_agents`, no `AgentTool`** (guarded by `test_router_has_no_sub_agents` /
  `test_router_has_no_agent_tools`).

Tier backbones are materialized through `src.config.resolve_model`, so each tier
keeps its endpoint wiring (Gemini-3 native/global, Claude via LiteLlm, Gemini-2.x
regional string → `LLMRegistry.new_llm`).

The five standalone `lite_agent`…`opus_agent` definitions remain in
`src/router/agents.py` but are NO LONGER the router's delegation targets — they are
kept only for standalone per-tier deploy/eval, as the source of each tier's
`INSTRUCTION` (imported by `tier_instruction_provider`), and as the GEPA
optimization sandbox roots (`src/router/<tier>_agent_opt/`).

## Deploy: tiers must be Gemini-2.5, passed as env overrides

Gemini-3 tier models don't complete MCP tool turns on this runtime, and the
config **defaults** for `LITE_MODEL`/`FLASH_MODEL`/`PRO_MODEL` are still Gemini-3.x
— so a *plain* `router --update` regresses the tiers. Deploy in place:

```bash
ENABLE_MEMORY_PRELOAD_CACHE=1 \
LITE_MODEL=gemini-2.5-flash-lite FLASH_MODEL=gemini-2.5-flash PRO_MODEL=gemini-2.5-pro \
CLASSIFIER_MODEL=gemini-2.5-flash-lite \
uv run python -m src.deploy.deploy_agents router --update
```

(sonnet/opus tiers stay Claude via LiteLlm.) Always `--update` in place; a
*recreate* mints a new SPIFFE identity that needs a fresh `agentregistry.viewer`
grant (see `agent-registry-mcp-resolution.md`).

## Latency levers (reduce empty-at-200 under load)

The router adds two per-turn costs the coordinator's direct path avoids, both
stacked ahead of the first streamed token:

- **Memory preload.** Switched from stock `PreloadMemoryTool` (a blocking 3-5s
  Memory Bank retrieve before *every* LLM hop) to `CachingPreloadMemoryTool` under
  `ENABLE_MEMORY_PRELOAD_CACHE` — one retrieve per invocation, zero
  cross-invocation staleness (same knob the coordinator uses; see
  `coordinator-latency-attribution.md`).
- **Classifier client.** `complexity._classifier_client()` caches the
  `genai.Client` process-wide; it was rebuilt (credential resolution + setup) on
  every request in `before_agent_callback`.

## Residual empties are platform-wide, not the router

After the fix the router still shows intermittent empty responses at HTTP 200.
A same-moment control proved this is not architectural:

| Run (2026-08-19, identical spaced prompts) | FULL | EMPTY |
|---|---|---|
| Router `6134…` | 2/6 | 4/6 |
| Coordinator probe `4380…` (same moment) | 2/6 | 4/6 |
| Coordinator probe `4380…` (~40 min earlier) | 6/8 | 0/8 |

On **both** engines the short `"meal expense limit"` synthesis succeeded while the
longer flight/hotel **search** synthesis emptied — the documented
high-complexity/latency empty-at-200 pattern (`online-quality-monitor.md`;
memory `online-helpfulness-dips-are-empty-streams`), equally present on the
reference healthy agent. `verify_mcp_tools` PASSes all three servers. Under rapid
probing the engine also scales out to many cold replicas (many worker PIDs in the
logs), adding cold-start empties on top of the regional spike. The coordinator
probe only looks perfect when the 5-min online-monitor cron has kept its replicas
warm.

**Conclusion:** the transfer_to_agent streaming defect is fixed — the router now
streams end-to-end on par with the coordinator. Driving empties to zero is a
warmth/regional concern (keep-warm `min_instances`, retry — `raw_stream.py` already
retries), orthogonal to the streaming architecture.

## Observability dashboard: the direct-tools fix also restored the model/tool tiles

The console **Observability** tab's per-model and per-tool tiles are **trace-derived**
(no `workload.googleapis.com/gen_ai*` metrics exist in this project). The per-model
tile aggregates `generate_content <model>` spans (emitted by
`opentelemetry-instrumentation-google-genai`, carrying `gen_ai.request.model`); the
per-tool tile aggregates `execute_tool <tool>` spans (emitted by ADK's
function-calling flow). Both are grouped per agent by the span's `service.name`
label, which on a deployed engine is the **reasoning-engine id**.

The old `transfer_to_agent` router showed **empty model/tool tiles** for the same
reason it didn't stream: the model + MCP work ran inside a *delegated sub-agent*
whose turn never completed on the managed runtime, so no `generate_content` /
`execute_tool` spans were emitted and attributed to the router engine. The
direct-tools rearchitecture fixed this as a side effect — the router now emits the
**same rich span tree as the coordinator**, attributed to its own engine
(`service.name = 6134089059699523584`). Confirmed live 2026-08-19 (deploy
`service.version=15`) — a router agent-run trace:

```
invoke_workflow router_agent           service.name=6134…
  invoke_agent router_agent
    router.route
      generate_content gemini-2.5-flash-lite   [classifier]
    call_llm                                   gen_ai.request.model=gemini-2.5-flash-lite
    generate_content gemini-2.5-flash-lite     [genai-instrumentation lib]
    execute_tool booking_mcp_book_flight
    execute_tool search_mcp_search_hotels
    call_llm / generate_content …              [synthesis hop]
```

**Residual caveat — Claude tiers (sonnet/opus) lack a dedicated `generate_content`
span.** `_should_emit_native_telemetry` (ADK `telemetry/tracing.py`) suppresses
ADK's own inference span whenever `_is_gemini_agent(agent)` is True; that check
reads `agent.model.model`, which for the router is `TierRoutingLlm`'s default
(`gemini-2.5-flash-lite`), so it returns True for **every** tier. For Gemini tiers
this is correct — the genai instrumentation lib emits the `generate_content <model>`
span. For Claude tiers the underlying model is LiteLlm (not `google.genai`), so the
genai lib doesn't wrap it *and* ADK suppressed its native span → no dedicated
`generate_content` span. The model is still recorded on the `call_llm` span
(`gen_ai.request.model=claude-…`, set by `trace_call_llm` from `llm_request.model`),
so the information is not lost. This is an inherent ADK+LiteLlm telemetry limitation
(the coordinator would hit it too if it ran on Claude), not a router regression;
fixing it fully would require ADK to evaluate the model family *per request* rather
than off the static `agent.model`. Making `TierRoutingLlm.model` request-dynamic
would reintroduce the concurrency race the stateless dispatcher exists to avoid, so
it is deliberately left as documented behavior.

Diagnose with a single tightly-bounded Cloud Trace read (the read quota is
300/min; unfiltered `COMPLETE`-view pagination burns it fast — fetch one page via
`itertools.islice` or the cheap `ROOTSPAN` view to collect ids, then `get_trace`).

## Measuring

- `src/eval/spike_router_streaming.py --agent-id <id> --repeat N` — categorizes
  each turn EMPTY / PARTIAL / FULL via the raw-SSE client.
- `src/eval/raw_stream.py` — client-side SSE fallback; the SDK `stream_query`
  skews on NDJSON (see `agent-engine-sse-stream-parse.md`).
