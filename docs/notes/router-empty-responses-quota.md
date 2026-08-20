# Router empty responses: an oversized tool payload burning the GenerateContent quota

**Status:** root cause found and fixed (2026-08-20). Two fixes shipped: bound the
expense tool's payload, and retry/label 429s in the router's tier dispatcher.

**Symptom.** The deployed 5-tier router engine `6134089059699523584` answered
roughly **40% of turns with nothing at all**: HTTP 200, a stream that yields
events but zero characters of text ("empty-at-200"). The same load pattern
against the coordinator probe engine in the same minute completed 15/16.

## Root cause

**Vertex rejects the router's tier calls with HTTP 429 `RESOURCE_EXHAUSTED`, and
an unhandled 429 surfaces to the caller as an empty stream.**

The quota pressure is self-inflicted, and it comes from the *expense MCP
server*, not from the router's own traffic rate:

1. `src/mcp_servers/expense/mock_db.py` keeps `expenses` as a module-level dict.
   It is an **unbounded in-memory accumulator** — every demo, eval, traffic run
   and bake-off appends to it and nothing ever prunes it. On a long-lived Cloud
   Run instance, `EMP001` had reached **96 records / 26,170 characters of JSON**,
   growing ~1.2KB per run.
2. `get_expenses` returned **every** matching record. So "list my expenses"
   handed the agent all 26KB.
3. The router is a **direct-tools** agent (it holds the MCP toolsets itself —
   see [router-transfer-streaming.md](router-transfer-streaming.md)), so it must
   absorb that payload *and* re-emit it in its answer. The coordinator, which
   *delegates*, never carries it on the root agent's context.
4. Measured per LLM hop: **router median 17,206 input tokens vs coordinator
   1,850**, with 14–21K-character answers taking 32–49s per turn.
5. That token burn tripped the per-minute `GenerateContent` quota.

### The evidence chain

- **Cloud Trace.** Router spans named `generate_content gemini-2.5-flash-lite`
  with `error.type = ClientError`, `input_tokens = 0`, `output_tokens = 0`,
  `finish_reasons = []`, duration 5.4–18.5s, and
  `code.function.name = google.genai.AsyncModels.generate_content`. A rejected
  call, not a slow one.
- **Cloud Monitoring**, `serviceruntime.googleapis.com/api/request_count` with
  `resource.labels.service="aiplatform.googleapis.com"`: **219 responses with
  `response_code = 429` in two hours; 215 of them carry
  `credential_id = oauth2:…/reasoningEngines/6134089059699523584`** and
  `grpc_status_code: '8'`. The 429s are attributable to this one engine.
- **Raw SSE probe** (`src/eval/raw_stream.py`, bypassing the array-only
  `google-api-core` REST parser): `http=200 bytes=46069 events=3 chars=0`. The
  server genuinely sends no text — this is not a client parse artifact.
- **Local in-process reproduction**: `elapsed=17.38s chars=0` running the router
  agent outside the managed runtime entirely, which rules out any
  container-kill / runtime explanation.

### Hypotheses that were falsified along the way

Recorded so nobody re-runs them:

| Hypothesis | Verdict |
| --- | --- |
| `min_instances=1` cold starts | **False.** Router and coordinator specs are identical (min=4, max=0, concurrency=0) and the empty rate did not move. |
| Managed-runtime worker churn / container kills | **False.** Coordinator 1.13 vs router 1.24 traces per worker — no meaningful difference — and the defect reproduces locally in-process. |
| SDK SSE parse skew (`agent-engine-sse-parse-skew`) | **False.** The raw-SSE client sees the same zero characters. |
| `litellm` memory footprint on the router workers | **False.** Real (308MB vs 168MB) and worth fixing on its own, but not the cause. |
| Cold-start import time | **False.** 6.15s router vs 5.36s coordinator. |
| "Residual empties are platform-wide" | **False.** Router 2/20 full vs coordinator 15/16 in the same minute. |

## Fixes

### 1. Bound the expense payload (removes the cause)

`src/mcp_servers/expense/mock_db.py` grew `MAX_EXPENSES_RETURNED = 20`, and
`get_expenses(user_id, limit=MAX_EXPENSES_RETURNED)` now returns the newest
`limit` records (clamped to the cap) plus `total_count` / `total_amount`
computed over the user's **entire** history and a `truncated` flag.

That shape matters: the agent can still answer *"you have 96 expenses totalling
$X"* honestly without the whole history entering its context. The MCP tool
docstring (`src/mcp_servers/expense/server.py:get_user_expenses`) tells the model
exactly that, so a truncated result is reported as truncated rather than passed
off as the complete list.

This changes the tool's return type from a bare list to a dict — callers read
`result["expenses"]`.

### 2. Retry and label 429s (removes the silence)

Bounding the payload lowers the pressure, but any shared-quota project can still
throttle. `src/router/tier_routing_llm.py` now wraps the forward in a retry loop:

- `_is_quota_error` duck-types the rejection (`code == 429`, or
  `RESOURCE_EXHAUSTED`/`QUOTA EXCEEDED` in the message) so a LiteLlm-wrapped
  Claude tier is covered by the same path as a native Gemini tier.
- Three attempts with exponential backoff (2s, 4s) — sized to ride out the
  bursty part of a per-minute window without stretching a demo turn.
- A turn that has **already streamed a chunk is never retried**: the client holds
  that partial output and a retry would duplicate it.
- When every attempt is throttled, the dispatcher yields an explicit
  `LlmResponse` carrying `THROTTLED_RESPONSE_PREFIX`
  (`"The model is temporarily rate-limited (RESOURCE_EXHAUSTED)…"`) plus
  `error_code="RESOURCE_EXHAUSTED"`. **Never silence.** The text is greppable on
  purpose so an eval scoring it sees a labelled infra failure rather than a
  low-quality answer — the same separation the online monitor makes with
  `infra_empty_rate` (see
  [online-infra-empty-and-baseline-alerts.md](online-infra-empty-and-baseline-alerts.md)).

## Deploying the fixes

Both need a deploy to take effect:

```bash
# 1. Expense MCP server (the cap)
uv run python -m src.deploy.deploy_mcp_servers

# 2. Router, IN PLACE, with the tier overrides — a bare
#    `deploy_agents router --update` regresses the tiers to Gemini-3.
ENABLE_MEMORY_PRELOAD_CACHE=1 \
LITE_MODEL=gemini-2.5-flash-lite FLASH_MODEL=gemini-2.5-flash \
PRO_MODEL=gemini-2.5-pro CLASSIFIER_MODEL=gemini-2.5-flash-lite \
uv run python -m src.deploy.deploy_agents router --update --min-instances 4
```

Never *recreate* the router engine: a new engine mints a new SPIFFE identity that
needs a fresh `roles/agentregistry.viewer` grant
([agent-registry-mcp-resolution.md](agent-registry-mcp-resolution.md)).

## Measured result (2026-08-20, after deploying both fixes)

Identical raw-SSE load curve (4 prompts x 3 rounds per spacing, lite + flash
tiers), run against the router right after the redeploy, with the **coordinator
probe engine as a same-period control**:

| | before | after |
| --- | --- | --- |
| Router full responses | **2/20 (10%)** | **17/24 (71%)** — 9/12 at 6s spacing, 8/12 at 4s |
| Coordinator control | 15/16 (94%) | **8/12 (67%)** |
| 429s attributed to engine 6134 | 215 in 2h | **0 after 14:27Z**, across the whole post-fix curve |
| Turn latency | 32-49s | 3-19s |
| Answer length ("list all expenses") | 14-21K chars | 21-140 chars |

The router-specific gap is closed: it now runs at or slightly above the
coordinator's rate in the same window. Local in-process runs of `router_agent`
(which reproduced `chars=0` before) now complete 3/3.

**The residual empty-at-200 is platform-level, not router-specific** — the
coordinator shows the same rate in the same period. That is the already-known
empty-at-200 behaviour the online monitor partitions out as
`agent_online_eval/infra_empty_rate`
([online-infra-empty-and-baseline-alerts.md](online-infra-empty-and-baseline-alerts.md)).
Its signature is distinct from the quota failure: the stream carries the
`function_call` event and then ends before the tool result comes back, while the
engine logs show the MCP `tools/call` completing *after* the client stream has
already closed. No 429 is involved.

## Verifying

- Re-run the load curve against the router and count empty streams.
- Re-query the 429 attribution: `serviceruntime.googleapis.com/api/request_count`
  filtered to `resource.labels.service="aiplatform.googleapis.com"`,
  `metric.labels.response_code="429"`, grouped by `resource.labels.credential_id`
  — the count against `reasoningEngines/6134089059699523584` should collapse.
- Grep router logs for `THROTTLED_RESPONSE_PREFIX`: any hit is a residual
  throttle that survived three retries, now visible instead of silent.

## Caveat

The in-memory `expenses` store is still unbounded — the cap is on what the *tool
returns*, not on what accumulates. That is deliberate (`total_count` /
`total_amount` stay truthful over the full history), and the store resets
whenever the Cloud Run instance recycles. If the payload ever needs to shrink
further, lower `MAX_EXPENSES_RETURNED`; the tool contract already advertises
truncation.
