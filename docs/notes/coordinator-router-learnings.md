# Porting the router's deployment fixes to the coordinator

**Status:** shipped 2026-08-20. Four changes to the coordinator + one MCP server
cap, derived from the router's empty-response investigation
([router-empty-responses-quota.md](router-empty-responses-quota.md)) and the
direct-tools rearchitecture ([router-transfer-streaming.md](router-transfer-streaming.md)).

**Trigger.** After the router (`6134089059699523584`) was fixed, its deployed
observability looked *better* than the coordinator's (`4380288848559603712`).
The question was which of those fixes the coordinator actually needed.

## What the measurement said

A live Cloud Trace census (`view=COMPLETE`, `service.name:<engine-id>`, last 3h)
over both engines, plus a 24h Cloud Monitoring 429 count
(`serviceruntime.googleapis.com/api/request_count`, `response_code="429"`,
grouped by `credential_id`):

| | coordinator 4380 | router 6134 |
| --- | --- | --- |
| custom domain span | **none** | `router.route` on 40/40 traces, 8 attributes |
| `generate_content <model>` span | present (`gemini-2.5-flash`) | present, per-tier |
| `session.id` / `user.id` on `invoke_agent` | present | — |
| HTTP 429s in 24h | **0** | 215 (all pre-fix) |
| `min_instances` | 4 | 4 |
| AgentTool (`travel_agent`/`expense_agent`) calls | **0 across 10 traces** | n/a |

So the gap was **not** infrastructure and **not** missing ADK instrumentation —
both engines emit the identical `invoke_workflow` / `invoke_agent` / `call_llm` /
`generate_content` / `execute_tool` / `tools/call` tree. The router simply
**publishes its decision inputs** to the trace and the coordinator published
nothing domain-specific.

## What transferred

### 1. Bounded tool payload (the same unfixed bug, one server over)

`src/mcp_servers/booking/mock_db.py`'s `bookings` dict is the **identical
unbounded accumulator shape** that grew `expenses` to 96 records / 26KB and
burned the router's quota, and `list_all_bookings` returned all of it. The
coordinator holds the booking toolset **directly**, so it would absorb and
re-emit that payload exactly as the router did.

`list_bookings(limit=20)` now returns
`{total_count, returned_count, truncated, bookings}` newest-first, with the limit
clamped to `MAX_BOOKINGS_RETURNED = 20` — the same contract as `get_expenses`.
The tool docstring instructs the model to report truncation honestly.

### 2. Quota retry, shared (`src/models/quota_retry.py`)

The 429 retry was welded into the router's `TierRoutingLlm`. It moved to
`RetryingLlm`, a transparent `BaseLlm` wrapper (`.model` still reports the real
backbone id, so billing, resource labels and the trace's `model.id` are
unaffected). `TierRoutingLlm._select` now returns a cached `RetryingLlm` per tier
and its `generate_content_async` is model-rewrite + plain forward. **All 17
pre-existing router tests pass unmodified** — that green suite is the proof the
refactor is behaviour-preserving.

The coordinator's model is now `retrying_model(COORDINATOR_MODEL)`.

> **This is insurance, not a fix.** The coordinator has taken **0 of the 215**
> observed 429s. It does not resolve anything currently broken; it removes a
> failure mode that has already bitten the other engine.

### 3. Domain spans + config attributes

ADK callbacks are point-in-time, so there is no place to wrap a whole coordinator
turn in a span — `before_agent_callback` returns before the turn runs. The router's
`router.route` works because it wraps a *discrete sub-operation*. Same approach:

- **`coordinator.memory_preload`** (in `CachingPreloadMemoryTool.process_llm_request`)
  with `memory.cache_hit` / `memory.result_count` / `memory.invocation_id`, and
  `memory.error` + a recorded exception on the failure path. The Memory Bank
  retrieve is 3–5s per invocation
  ([coordinator-latency-attribution.md](coordinator-latency-attribution.md)) and
  had **zero** trace presence; `memory.cache_hit` is also the only way to see
  from a trace whether the per-hop collapse actually happened.
  **Limitation:** this span lives on the caching subclass, so it exists only when
  `ENABLE_MEMORY_PRELOAD_CACHE=1`. Engine 4380 has it; a default deploy will not.
- **`coordinator.memory_save`** in `save_memories_callback`. The write was wrapped
  in a bare `contextlib.suppress(Exception)` — a Memory Bank write failure was
  *completely* invisible. `suppress` is now the **outer** manager and `traced` the
  inner one, so `traced` records the exception and sets ERROR status before
  `suppress` swallows it. Turn behaviour is unchanged.
- **Config attributes on `invoke_agent`** alongside the existing `session.id` /
  `user.id`: `model.id`, `memory.enabled`, `memory.cache_enabled`,
  `armor.server_side`. `model.id` is the operationally important one — the
  backbone moves with `COORDINATOR_MODEL` (bake-off, DOE points, the 2.5 pin) and
  a trace previously could not say which one served a request without reading the
  `generate_content` span's *name*. `armor.server_side` comes from the new
  `src/armor/config.py:server_side_armor_enabled(model)`, which is the same gate
  `get_armored_generate_config` applies (not a second copy of the predicate).

### 4. Direct tools only — both AgentTools removed

The census settled a structural question the router had already answered for
itself: the coordinator's `travel_agent` / `expense_agent` `AgentTool`s fired **0
times across 10 traces**. Section 1 of the instruction drives everything through
the coordinator's own direct MCP tools. They cost tool-definition tokens on every
hop and, if one ever did fire, would land on the non-streaming nested-MCP path
the direct-toolset design exists to avoid.

The delegation lines were **hand-adapted glue, not GEPA output** (the header
comment in `coordinator_agent.py` said so), so removing them is deleting
hand-written glue. The edits were surgical deletions; every other sentence is
byte-for-byte the optimizer's:

1. Section 2 (**Delegation**) deleted entirely; sections renumbered 3/4/5 → 2/3/4.
2. Its capability was **folded into** the "User Expense Retrieval" bullet
   ("...including questions about expense status, appeals, and detailed expense
   reporting") rather than lost.
3. Section 5's "offering to use a specialist agent tool for booking" → "offering
   to book a listed option or another relevant next step".
4. The closing paragraph's "use the appropriate specialist agent tool
   immediately" sentence deleted.
5. (Beyond the three planned deletions) Section 3's "proceed with direct tool
   usage **or delegation as appropriate**" → "proceed with direct tool usage" —
   it instructed a capability that no longer exists.

**`travel_agent` and `expense_agent` keep their own MCP toolsets and are
untouched.** They are independently deployed and scored
(`multi_agent_batch_eval --agents travel_agent`, `simulated_eval`,
`one_time_eval`, their own evalsets, `run_all_evals.py`). Two deployables, not
duplication.

#### Before/after rubric A/B

This substitutes for a re-optimization run, and it is **weaker evidence** than
one: the resulting prompt has not been through GEPA. Both runs are
`multi_agent_batch_eval --agents coordinator_agent --agent-id 4380288848559603712`,
49 cases, against the *deployed* engine.

| metric | before | after agent | after descriptors |
| --- | --- | --- | --- |
| `final_response_match_v2` | 0.71 | 0.66 | 0.68 |
| `final_response_quality_v1` | 0.81 | 0.77 | 0.82 |
| `hallucination_v1` | 0.69 | 0.75 | 0.68 |
| `instruction_following_v1` | 0.63 | 0.70 | 0.63 |
| `safety_v1` | 1.00 | 0.97 | 0.98 |
| `tool_use_quality_v1` | 0.42 | 0.38 | 0.38 |
| **mean** | **0.710** | **0.705** | **0.695** |

**Verdict: flat.** The mean moved −0.005 then −0.010 — a percentage point in
total across two changes, on 49 cases scored by a non-deterministic autorater
(three unpaired judge runs). Nothing here justifies re-optimization; nothing here
proves the change helped either.

The third column is what makes that reading defensible rather than hopeful.
Run 2's two apparent *gains* (`hallucination_v1` +0.06, `instruction_following_v1`
+0.07) **both returned to their exact pre-change values** in run 3 — a run where
the agent was byte-identical to run 2. So those gains were judge noise, and by
the same token so are the drops of similar size. The measured run-to-run spread
on this harness is roughly ±0.07 per metric; only a move larger than that means
anything.

Two honest caveats on this table:

- It is **not a clean A/B**. The "after" engine also carries the quota wrapper,
  the new spans, the booking cap and the sub-agent model pin. None of those
  plausibly change the coordinator's answers (the retry only fires on a 429, of
  which it has had none; the sub-agents are unreachable), but they are not
  isolated.
- `tool_use_quality_v1` is the **delegation-blind SDK metric** that structurally
  penalizes this coordinator's topology; the repo's own `geap_tool_use` judge
  exists precisely because of it
  ([coordinator-tool-use-quality.md](coordinator-tool-use-quality.md)). Its 0.38
  is not a quality signal about this change. Note it did **not** recover when the
  eval descriptors stopped declaring sub-agents (0.38 → 0.38), which is further
  evidence the metric's problem is the rubric, not the declared topology.

#### The eval descriptors (run 3)

`src/eval/batch_eval.py:_build_agent_info` and
`src/eval/agent_eval_configs.py:_build_coordinator_info` still declared
`sub_agents=["travel_agent", "expense_agent"]` with instructions about routing to
specialists. Both are now **single-agent** descriptors whose instruction lists the
coordinator's own MCP tools, and the nested `travel_agent`/`expense_agent`
`AgentConfig` entries are gone — they were unreachable from the root agent and are
still described independently by `_build_travel_info` / `_build_expense_info`.

These were changed **after** the A/B on purpose: they feed the delegation-aware
`geap_tool_use` judge, so moving them with the agent would have confounded run 2.
Run 3 is the neutrality check, and it is neutral.

## What did NOT transfer

- **`min_instances`.** Raising the router's floor 1→4 measurably moved its
  Gemini-tier empty rate, so it stays as a fix — but read the original claim here
  ("containers hard-killed mid-invocation at `min_instances=1`") as **superseded**.
  It rested on failing traces lacking their enclosing `invoke_workflow`/`invoke_agent`
  span, and that inference was falsified: the same empty reproduces **locally,
  in-process**, where there is no container to kill. The empties that remained were
  the 429/unbounded-payload issue
  ([router-empty-responses-quota.md](router-empty-responses-quota.md), whose
  falsified-hypotheses table is scoped to that residual). Either way the coordinator
  already runs at 4, so nothing to port. Four separate causes produced this one
  symptom — [empty-at-200-field-guide.md](empty-at-200-field-guide.md) tells them
  apart and flags the span-shape trap.
- **Tier model overrides.** Coordinator-irrelevant.
- **The lazy-litellm import.** The coordinator never imported litellm at 168MB
  baseline; that was the router closing the gap *to* the coordinator.
- **Residual empty-at-200.** It hits both engines, involves no 429, and is already
  partitioned out as `agent_online_eval/infra_empty_rate`
  ([online-infra-empty-and-baseline-alerts.md](online-infra-empty-and-baseline-alerts.md)).
  None of this work is expected to move it.

## Deploy

The probe engine is updated **in place** — never recreated (a recreate mints a new
SPIFFE identity that needs a fresh `roles/agentregistry.viewer` grant):

```bash
uv run python -m src.deploy.deploy_mcp_servers    # booking cap, server-side
ENABLE_MEMORY_PRELOAD_CACHE=1 \
COORDINATOR_MODEL=gemini-2.5-flash \
TRAVEL_MODEL=gemini-2.5-flash EXPENSE_MODEL=gemini-2.5-flash \
uv run python -m src.doe.deploy_coordinator --update 4380288848559603712
```

`TRAVEL_MODEL`/`EXPENSE_MODEL` were still on `gemini-3.5-flash`. The coordinator no
longer reaches either sub-agent, so this pin is consistency with the 2.5 backbone
the rest of the demo runs on — **latent-only, not a fix.**
