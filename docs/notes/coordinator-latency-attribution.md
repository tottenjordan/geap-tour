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

## Follow-up (not yet done): memory-preload per-invocation cache

The Memory Bank retrieve is the other 3–5s controllable slice, but it is
**load-bearing for the validated cross-session-recall demo**, so it is deliberately
left as a separate, careful change. Candidate: a short-TTL per-`(user, query)`
cache in front of `PreloadMemoryTool.search_memory` so a multi-turn request does
not re-pay the network retrieve on every internal LLM hop. Must be validated
against `verify_cross_session_recall` before shipping.
