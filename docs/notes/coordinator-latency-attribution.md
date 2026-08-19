# Coordinator latency — attribution & the thinking-budget knob

**Problem.** The managed `reasoning_engine/request_latencies` metric reports the
coordinator at **~17s p50 / 51s p95** (probe engine `4380288848559603712`,
`gemini-2.5-flash`). After the session-creation ceiling was lifted (session reuse,
PR #50), this latency is the sole remaining limiter on sustained throughput — a
2-tool booking chain achieved only ~0.35 QPS because requests take so long that
they back up.

## Where the time actually goes (measured, not assumed)

`src/eval/latency_probe.py` streams a turn against the live engine, stamps every
SSE event with `time.monotonic()`, and buckets the inter-event gaps:

- **`startup`** — request start → first event (client guardrail + Memory Bank
  preload + server-side Model Armor screen + the first LLM call + any cold-start).
- **`mcp_tool`** — a `function_call` event → its `function_response` (the MCP
  round-trip to Cloud Run).
- **`llm`** — a `function_response` → the next model event (a generation hop).

Two live runs on the probe engine (2 rounds each):

| prompt | user | startup | mcp | llm |
|---|---|---|---|---|
| `Hello!` | empty | 3.6–7.3s | — | — |
| `Search flights JFK→LAX` | empty | 5.0–5.8s | 0.2s | 1.7–2.0s |
| `Book FL001 … then hotel <$350` | empty | 4.8–12.6s | 0.3–1.1s | 4.7–5.0s |
| `Hello!` | `alice` (seeded) | 8.8s | — | — |
| `Search flights` | `alice` | 10.6s | — | — |
| multi-tool | `alice` | 13.3s | — | — |

**Findings:**
1. **MCP tools are cheap** — 0.2–1.1s per hop. The Cloud Run tool servers are
   *not* the bottleneck (this refuted the initial theory).
2. **`startup` (time-to-first-event) dominates** — 3.6–13.3s, the single largest
   controllable slice.
3. **Memory Bank preload is real and per-invocation** — the seeded user `alice` is
   consistently **3–5s slower** than an empty user. ADK's `PreloadMemoryTool` runs
   a **blocking** `search_memory` before *every* LLM turn (keyed on
   `user_content.parts[0].text`), so a multi-turn query pays it repeatedly.
4. **Uncapped "thinking"** — `gemini-2.5-flash` runs with default (uncapped)
   thinking, which inflates both first-token and per-hop generation time.

## The shipped lever: opt-in generation-config knobs

`src/config.py` adds two **opt-in, env-tunable** knobs (default unset → behavior
unchanged):

- `COORDINATOR_THINKING_BUDGET` — `0` disables thinking, `-1` = dynamic, `N` = token
  cap.
- `COORDINATOR_MAX_OUTPUT_TOKENS` — caps response length.

`src/armor/config.py:get_armored_generate_config(model)` attaches them **only on
the regional-Gemini path** (`_is_regional_gemini`), the same gate that attaches
Model Armor — because that is where the probe's `gemini-2.5-flash` runs, and
because Gemini-3 (native/global) and Claude (LiteLlm) resolve generation config
differently. An unset knob yields a byte-identical config to before, so nothing
changes until an operator opts in.

**Why opt-in, not a new default:** flipping the default would silently change the
served engine on the next `deploy_coordinator --update` (cf. the model-revert trap
in the probe-engine memory), and disabling thinking may cost quality on the
multi-step/adversarial cases. The knob is a dial; the recommended validation is a
live A/B (below) before committing to a value.

### Recommended live A/B (before changing the served default)

Deploy a probe *revision* in place (never recreate, never repoint `.env`) with the
model split pinned and the knob set, then re-measure latency and offline quality:

```bash
COORDINATOR_THINKING_BUDGET=0 \
COORDINATOR_MODEL=gemini-2.5-flash AGENT_MODEL=gemini-3.5-flash \
TRAVEL_MODEL=gemini-3.5-flash EXPENSE_MODEL=gemini-3.5-flash ROUTER_MODEL=gemini-3.1-flash-lite \
  uv run python -m src.doe.deploy_coordinator --update 4380288848559603712 \
    --min-instances 4 --display-name coordinator-gemini25-probe

uv run python -m src.eval.latency_probe --agent-id 4380288848559603712      # latency delta
uv run python -m src.eval.multi_agent_batch_eval --agents coordinator_agent \
    --agent-id 4380288848559603712 --threshold 3.0                          # quality guard
```

If quality holds, bake the chosen `COORDINATOR_THINKING_BUDGET` into the deploy;
if it dips, dial the budget back up.

### A/B result (2026-08-18, `COORDINATOR_THINKING_BUDGET=512`)

Ran the A/B live on the probe engine (in-place `--update` revision, model split
pinned). **Latency — clear win**, most visible on the multi-tool booking case's
`llm` bucket (where thinking lands) and warm totals:

| multi-tool booking | baseline | thinking=512 |
|---|---|---|
| `llm` bucket | 3.6s / **23.2s** | 9.5s / 8.8s / **5.7s** |
| warm total | **36.0s** | **13.2s** |

The ~23s uncapped-thinking tail is eliminated; the `llm` bucket is bounded to
5.7–9.5s and the warm multi-tool total dropped ~2.7×. (Startup still varies with
cold-start; the cap targets generation, not startup.)

**Quality — holds.** `multi_agent_batch_eval` on the capped engine (49 cases):
`final_response_quality` 0.79, `hallucination` 0.73, `instruction_following` 0.64,
`safety` 0.98, `final_response_match` 0.70 — all PASS (floor 0.60). The lone FAIL,
`tool_use_quality_v1` 0.38, is the **delegation-blind SDK rubric** documented in
[coordinator-tool-use-quality.md](./coordinator-tool-use-quality.md) (it penalizes
`transfer_to_agent` routing; the *published* series uses `geap_tool_use` instead),
i.e. a pre-existing measurement artifact, not a thinking-cap regression.

**Decision:** the probe engine is left on `COORDINATOR_THINKING_BUDGET=512`. Note
this is now a **baked env var** — a bare `--update` without it silently reverts to
uncapped thinking (same class as the model-revert trap).

## The second lever: opt-in memory-preload cache (`ENABLE_MEMORY_PRELOAD_CACHE`)

The Memory Bank retrieve is the other 3–5s controllable slice. Root cause (from
ADK source, `google/adk/tools/preload_memory_tool.py`): `PreloadMemoryTool`
runs `process_llm_request` **before every LLM hop** and each time issues
`tool_context.search_memory(user_query)` — a blocking Vertex Memory Bank round-trip
— where `user_query = user_content.parts[0].text`, the invocation's *original*
message, **constant across all hops**. So a multi-hop request (initial → after a
tool → after the next tool → final) re-issues the *identical* retrieve every hop.
The latency table above shows the cost: seeded user `alice` is consistently 3–5s
slower than an empty user, and a multi-tool turn pays it on each internal hop.

`src/agents/caching_preload_memory_tool.py:CachingPreloadMemoryTool` subclasses
`PreloadMemoryTool` and memoizes the retrieve keyed by `(invocation_id, query)`:

- **Within one invocation** every hop shares the same key → the network retrieve
  happens **once**; subsequent hops hit the cache and re-render from it.
- **Across invocations** a new `invocation_id` (`ReadonlyContext.invocation_id`)
  always misses → **zero cross-invocation staleness by construction**. A fact added
  to Memory Bank between two requests can never be masked by a stale entry — the
  exact property the validated cross-session-recall demo depends on. (This is why
  keying on `invocation_id` is preferred over a TTL cache at the memory-service
  seam, which the service layer can't scope to an invocation.)
- Only *successful* retrieves are cached (an empty-memories result included); a
  transient `search_memory` exception is not cached, so a later hop retries.
- Being a subclass, `deploy._wants_memory()` still detects it (isinstance) and
  provisions the Memory Bank service.

**Opt-in, default off** (`src/config.py:ENABLE_MEMORY_PRELOAD_CACHE`, mirrors the
thinking-budget knob): with the flag unset the coordinator wires the stock
`PreloadMemoryTool` — byte-identical behavior, so a bare `deploy_coordinator
--update` changes nothing. `src/agents/coordinator_agent.py:_build_memory_tools`
selects the tool.

### Recommended live validation (recall MUST hold before baking in)

Deploy a probe *revision* in place (never recreate, never repoint `.env`) with the
model split pinned, the thinking budget preserved, and the cache flag on:

```bash
COORDINATOR_THINKING_BUDGET=512 ENABLE_MEMORY_PRELOAD_CACHE=1 \
COORDINATOR_MODEL=gemini-2.5-flash AGENT_MODEL=gemini-3.5-flash \
TRAVEL_MODEL=gemini-3.5-flash EXPENSE_MODEL=gemini-3.5-flash ROUTER_MODEL=gemini-3.1-flash-lite \
  uv run python -m src.doe.deploy_coordinator --update 4380288848559603712 \
    --min-instances 4 --display-name coordinator-gemini25-probe

uv run python -m src.eval.verify_cross_session_recall --user-id alice \
    --engine-id 4380288848559603712                                    # MUST print RECALL: PASS
uv run python -m src.eval.latency_probe --agent-id 4380288848559603712 # latency delta
```

### Live validation (2026-08-19, `ENABLE_MEMORY_PRELOAD_CACHE=1`)

Deployed the probe engine `4380288848559603712` in place as a new revision with
the cache flag on (model split pinned, `COORDINATOR_THINKING_BUDGET=512` preserved,
`--min-instances 4`).

- **Recall holds — the load-bearing gate.** `verify_cross_session_recall
  --user-id alice` printed **`RECALL: PASS`**: a preference stated in session A
  ("window seats / Delta / Marriott corporate rate") resurfaced verbatim in a
  brand-new session B via `PreloadMemoryTool`. The invocation-scoped key means the
  session-B probe (a fresh `invocation_id`) always misses the cache and does a live
  retrieve, so the cache cannot mask a newly-persisted fact — confirmed live, not
  just by construction.
- **Latency healthy** (`latency_probe --user-id alice`, seeded user): warm
  multi-tool booking **7.9s total** (startup 4.2s, mcp 0.3s, llm 3.0s), with the two
  real domain tools surfaced. That warm startup (4.2s) sits in the *empty-user*
  range from the table above — i.e. the seeded-user preload penalty (previously
  +3–5s, re-paid per hop) no longer dominates a warm turn. (Single combined run:
  the thinking cap and the cache are both on, so this is the shipped-config number,
  not an isolated cache-only delta.)

**Decision:** implementation shipped behind the default-off flag. The probe engine
is left with the flag **on** as the reference revision; recall + latency both pass.
Enabling it on any other deploy is an opt-in env var, and recall must be re-checked
if the coordinator backbone or ADK's `PreloadMemoryTool` changes.

