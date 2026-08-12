# Coordinator `tool_use_quality`: root cause & the eval-harness fix

The coordinator's `tool_use_quality_v1` metric read ~0.34–0.42 across several
pipeline runs — well under the 0.60 bar and the worst of the six metrics. The
investigation found the low number was **mostly a measurement artifact**, not an
agent defect. This note records what was actually wrong (all evidence-backed, not
theorized) so nobody re-chases it.

## Three independent contributors

### 1. DOMINANT: ~50% empty turns from SDK concurrency + cold start

The Vertex evals SDK fires **all** eval prompts at the engine concurrently:
`vertexai._genai._evals_common.AGENT_MAX_WORKERS = 20` (hardcoded, read at call
time in `_execute_inference_concurrently`). Against a cold or single-instance
Agent Engine, ~half the `stream_query` calls complete *normally but yield no
content events* — an "empty turn". Signature of a dropped item:

```json
{"candidate":"agent_engine_0","agentData":{"turns":[{"turnIndex":0,"turnId":"turn_0"}]}}
```

No events → the judge template errors `Variable response is required but not
provided` → the item is silently dropped from the metric. Measured: **10 of 20
items empty** on a cold run. A serial warm re-query recovered **9 of 10**, proving
the prompts themselves are fine — it's load, not content.

Worse, the SDK's own retry does **not** cover this: `_execute_agent_run_with_retry`
only retries on *exceptions* (ResourceExhausted / generic). A normal completion
yielding `responses == []` is returned immediately with no retry.

**Fix** (`src/eval/_sdk_patches.py`, monkeypatch — all resolved at call time):
- Throttle `AGENT_MAX_WORKERS` → `EVAL_AGENT_MAX_WORKERS` (default **4**).
- Wrap `_execute_agent_run_with_retry` with `_run_with_empty_retry` — retries an
  empty (`[]`) result up to `EVAL_EMPTY_RETRIES` (default 4) with backoff; an
  error dict is *not* retried (already handled internally).
- `warm_agent_engine(engine, n=2)` pings the engine before inference so the first
  real batch doesn't hit a cold container.

Result: empty drops **10 → 2**, items scored **10/20 → 18/20**,
`tool_use_quality` **0.39 → 0.46** — now a *faithful* reading, not a floor.

The residual **2 empties are stochastic**, not per-prompt: a serial 3× probe of
the two suspect prompts ("Can you help me with an expense report?",
"Search for hotels in New York under $350 per night") returned content on every
attempt — the first is a valid text-only clarifying turn (no tool call), the
second a full tool-call+text turn. The tail-2 are the rare drop even 4 retries
miss under concurrent load; not worth chasing further.

### 2. The metric is a dynamic rubric with an unwinnable ceiling

`tool_use_quality_v1` is a **dynamic-rubric** metric: the judge *generates*
per-prompt ideal-behavior criteria, then scores `criteria_passed / total` (hence
quantized values like 0.25 = 1/4, 0.857 = 6/7). The generated criteria can be
**mutually contradictory** — e.g. an "Atlantis" (nonexistent destination) prompt
was scored against both `INTENT:SEARCH_HOTELS` (call the tool) *and*
`TECHNICAL_CORRECTNESS:NO_TOOL_CALL` (don't call it). No agent behavior satisfies
both, so the metric has a hard ceiling below 1.0 that no prompt or model change
can lift. Read a low score here as "acceptable," not "broken."

### 3. A genuine (smaller) behavioral gap

After the harness fix, the residual shortfall is real: the coordinator sometimes
calls a tool without validating inputs / asking for missing info. That behavior
lives in the **GEPA-optimized prompt** and per CLAUDE.md must be changed via
re-optimization, **not** a hand-edit. Left for a future GEPA pass; the DOE
screening exists to confirm `prompt_variant` is the lever (early smoke already
shows prompt_variant moving `tool_use_quality` +0.07 and `final_response_match`
+0.19).

## Related: the nested-delegation runtime limitation (the "booking flatten")

Distinct but discovered alongside the above, and the reason `final_response_match`
was also low (0.42). On the **managed** Agent Engine runtime, `AgentTool`
delegation to a sub-agent that then makes a **nested MCP call** does not stream
back through the deployed runtime — the stream dies with no final text (looks like
yet another empty turn). It works in-process locally; it stalls only on managed
runtime.

**Fix ("flatten"):** hold the MCP toolsets **directly** on the coordinator instead
of reaching them through a sub-agent. `coordinator_agent.py` now carries the
search / booking / expense toolsets itself and calls `book_flight` / `book_hotel`
directly; `travel_agent` / `expense_agent` remain only for conversational
hand-offs. This took `final_response_match` **0.42 → 1.00**.

## How to run a faithful coordinator eval

```bash
# NB: --agent-id defaults to AGENT_ENGINE_ID (the ROUTER, 4709...). To eval the
# coordinator you MUST pass its engine explicitly, or you measure the wrong agent.
uv run python -m src.eval.multi_agent_batch_eval \
  --agents coordinator_agent --agent-id 3631354304276725760
```

The patches apply automatically (`patch_evals_sdk()` + `warm_agent_engine()` are
wired into `multi_agent_batch_eval._run_single_agent_eval`). Tunables via env:
`EVAL_AGENT_MAX_WORKERS`, `EVAL_EMPTY_RETRIES`, `EVAL_EMPTY_BACKOFF`.
