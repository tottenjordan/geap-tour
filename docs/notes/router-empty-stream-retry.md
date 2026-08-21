# The residual empty-at-200: wrapping LiteLlm strips Anthropic's tool-call ids

**Status:** root-caused, fixed, verified live by a local A/B (§4), and now
**deployed** — the `AnthropicError: 'tool_call_id'` tracebacks are gone from
engine `6134089059699523584`'s logs and have not recurred.

> **This was not the dominant cause.** After deploying it the Claude tiers still
> failed **100%** of the time, including on prompts that call no tools at all —
> which no tool-call-id bug can explain. The real cause was an **OOM kill at the
> default 4Gi**, written up in
> [router-claude-tier-oom.md](router-claude-tier-oom.md); raising the container
> memory limit took the same probes from 8/8 empty to 0/8. This note's last
> caveat ("fixing the ids may reveal further Claude-tier issues") is the one that
> came true. The id fix is still correct and still shipped — it removed a real
> crash — it just was not what was emptying the streams.

Follows [router-empty-responses-quota.md](router-empty-responses-quota.md),
which fixed the *quota* cause of the router's empty streams and closed with:

> The residual empty-at-200 is platform-level, not router-specific — the
> coordinator shows the same rate in the same period.

**That conclusion was wrong.** The residual is router-specific, deterministic,
and confined to one tier.

## 1. Measuring it first

`spike_router_streaming --repeat 3` and a hammer on the one prompt that had
failed gave 23/24 full turns, which read as "the router is healthy, the residual
is rare and intermittent". A wider, tier-labelled sweep says otherwise
(`verify_router_health --repeat 4`, 28 turns against router
`6134089059699523584`):

```
  turns:            28
  full:             24 (85.7%)
  silent empty:     4 (14.3%) 95% CI [5.7%, 31.5%]
  labelled failure: 0 (throttled=0, empty=0)
  latency:          p50 7.7s / p95 18.4s

  by tier:
    flash  n=  8 full=100.0% empty=  0.0%
    high   n=  8 full= 50.0% empty= 50.0%
    lite   n=  8 full=100.0% empty=  0.0%
    pro    n=  4 full=100.0% empty=  0.0%
```

**20/20 clean on lite/flash/pro; 4/8 empty on high.** Not intermittent at all —
the earlier probe sets simply under-sampled the one band that fails. The failing
turns also carried `events=0` or `events=1`, not the `events=4`-with-both-MCP-calls
shape the previous note predicted.

Classifying the probes locally identifies the band:

```
high  score=0.40 tier=lite    model=gemini-3.1-flash-lite | Book flight FL001 …, then find a hotel …
high  score=0.75 tier=sonnet  model=claude-sonnet-4-6     | Show expense history …, check the policy …, submit …
```

Score 0.75 lands in the **0.60–0.80 sonnet band**. The router was deployed with
`LITE_MODEL`/`FLASH_MODEL`/`PRO_MODEL` overridden to Gemini-2.5 but **sonnet and
opus left on Claude** — so "high" is the only probe band that reaches a Claude
tier, and it is the only band that empties.

## 2. Root cause

Cloud Logging on the engine has the exception in full:

```
File ".../litellm/litellm_core_utils/prompt_templates/factory.py", convert_to_anthropic_tool_result
File ".../litellm/llms/anthropic/chat/transformation.py", transform_request
litellm.llms.anthropic.common_utils.AnthropicError: 'tool_call_id'
File "/code/src/router/tier_routing_llm.py", line 146, in generate_content_async
```

A quoted `'tool_call_id'` is a `KeyError` string re-wrapped as `AnthropicError`:
litellm subscripts `tool_message["tool_call_id"]` and the key is absent.

Why it is absent — ADK, `flows/llm_flows/contents.py`:

```python
# Anthropic and LiteLLM-backed providers (e.g. OpenAI) pair tool
# calls with their results by id, so `adk-*` fallback ids must
# survive replay.
if isinstance(canonical_model, tuple(id_pairing_model_types)):  # AnthropicLlm, LiteLlm, …
    preserve_function_call_ids = True
```

ADK mints synthetic `adk-<uuid>` ids, then strips them before replaying history
(Gemini rejects ids it never issued) **unless** this `isinstance` says the
provider pairs by id. Our Claude backbones never satisfy it:

```
resolved backbone      : LiteLlm      | isinstance LiteLlm = True
wrapped in RetryingLlm : RetryingLlm  | isinstance LiteLlm = False
```

The router's agent model is a `TierRoutingLlm` dispatcher and each tier sits
inside a `RetryingLlm`, so `agent.canonical_model` is a wrapper. ADK strips the
ids; the hop that replays a tool *result* to Claude raises; ADK yields nothing;
the caller gets HTTP 200 and zero characters.

### The precondition: a *mixed-tier* session, not any Claude turn

The first attempt to reproduce this came back clean, which corrected a claim
this note originally got wrong. ADK strips **only** ids carrying its own prefix
(`functions.py:AF_FUNCTION_CALL_ID_PREFIX = 'adk-'`, applied in
`contents.py:_copy_content_for_request`):

```python
if fc and fc.id and fc.id.startswith(AF_FUNCTION_CALL_ID_PREFIX):
    new_part.function_call = fc.model_copy(update={"id": None})
```

Claude issues its own ids (`toolu_vrtx_…`) and LiteLlm records them on the
`FunctionCall`, on both the streaming and non-streaming paths. Those never match
the prefix, so **a session that only ever ran on Claude survives the strip.**

The id that goes missing has to have been minted by ADK, which only happens for
a provider that issues none — **Gemini**. So the failing shape is a *mixed-tier
session*:

1. an earlier turn routes to a Gemini tier → no provider id → ADK mints
   `adk-<uuid>` and records it in the session events;
2. a later turn in the **same session** scores into the sonnet band → ADK strips
   that `adk-` id on replay → LiteLLM finds no `tool_call_id` → empty stream.

That is not an exotic path: the traffic generator deliberately **reuses one
session per user** (`docs/notes/traffic-session-reuse.md`), and a 5-tier router
exists precisely to send consecutive turns to different tiers. Mixed-tier
sessions are the normal case.

The full chain, and why every link was needed to see it:

| link | why it hid the cause |
| --- | --- |
| only Claude tiers affected | probe sets that didn't span complexity missed it entirely |
| only *multi-step* turns affected | a Claude turn with no tool result never replays one, so it works |
| only *mixed-tier* sessions affected | a pure-Claude repro comes back clean — the first A/B did exactly that |
| exception, not silence, at the model | the retry wrapper's 429 path doesn't fire; only the ADK layer sees it |
| ADK converts it to an empty stream | nothing in the *response* says an exception happened |

Note the ordering: `min_instances=4` and the tool-payload cap were real fixes for
real causes (they took the router from ~40% empty to ~14%), but they left this
untouched — it was never a capacity or quota problem.

### Two engines, one bug

This is not router-only. The bake-off's Claude coordinator resolves through
`retrying_model('claude-sonnet-5')` → `RetryingLlm` → `LiteLlm`, hitting the same
`isinstance` miss. Any tool-using turn on a Claude coordinator would empty the
same way, which would have shown up as a spurious quality loss for the candidate
backbone in a Gemini-vs-Claude comparison.

## 3. The fix

`src/models/tool_call_ids.py:restore_tool_call_ids(llm_request)` re-pairs every
unidentified function call/response, called from `RetryingLlm` once per turn when
`is_litellm_backed(inner)`. Both engines inherit it — the coordinator through
`retrying_model()`, the router through `TierRoutingLlm._select`.

Decisions worth keeping:

* **Restore rather than unwrap.** ADK's check reads the *agent's* model, so even
  an unwrapped tier wouldn't satisfy it through the dispatcher — and the wrappers
  earn their keep (per-tier dispatch, 429 retries).
* **Gate on the backbone, not on everything.** Gemini rejects ids it never
  issued, so stamping ids unconditionally would break the three healthy tiers to
  fix one. `is_litellm_backed` reads `sys.modules` instead of importing litellm,
  preserving the lazy import that keeps a Gemini-only worker at ~168MB instead of
  ~308MB.
* **Pair by name, in order** — ADK's own rule when ids are absent — so two hops on
  the same tool stay distinct instead of collapsing onto one id.
* **Replace the nested object, never mutate it.** ADK shallow-copies parts; the
  `FunctionCall` underneath is still shared with the session event, so writing
  `.id` in place would rewrite recorded history.
* **Only fill what's missing**, so a provider that supplied real ids keeps them
  and a retry can't renumber ids the provider has already seen.
* **An orphan response still gets an id.** A fabricated pairing degrades one
  reply; a missing key empties the whole stream.

## 4. Verifying it without a deploy

The router redeploy is blocked here, so the fix was proved where it can be
proved: locally, against **real Claude on Vertex**, driving the router's own
`TierRoutingLlm` and tier-selection callback over one `InMemorySessionService`
session. `src/eval/spike_tool_call_ids.py` runs both arms — the pre-fix arm makes
the gate report "not LiteLlm-backed", which is exactly what shipped before.

```
Mixed-tier session: gemini-2.5-flash (turn 1) -> claude-sonnet-4-6 (turn 2)

=== fix DISABLED (pre-fix)
  ids in history : [('call', 'adk-b60be219-…'), ('resp', 'adk-b60be219-…')]
  claude turn    : RAISED "BadRequestError … Vertex_aiException BadRequestError - 'tool_call_id'"

=== fix ENABLED
  claude turn    : FULL 'The total expense for EMP002 is $1,234.56.'

VERDICT: PASS — the fix turns the failing Claude turn into a real answer.
```

LiteLLM prints the payload it rejected, which is the mechanism in one line — the
assistant tool call arrives with `'id': ''` and the tool message has **no
`tool_call_id` key at all**:

```
{'role': 'assistant', 'tool_calls': [{'type': 'function', 'id': '', 'function': {'name': 'get_expense_total', …}}]}
{'role': 'tool',      'content': '{"employee_id": "EMP001", "total_usd": 1234.56}'}
```

The spike is kept (not deleted) as the re-run tool: it needs no engine, so it
still works when a redeploy is unavailable, and it re-checks the claim whenever
ADK or LiteLLM moves.

## 5. Also fixed: a genuinely silent turn

Separately from the above, `RetryingLlm` only ever retried an **exception**. An
inner generator that completed having produced no content raised nothing, so
`streamed` stayed `False` and the generator returned — HTTP 200, zero characters,
no retry, no log line. That path is now retried on the same budget and, if it
survives, ends in a greppable `EMPTY_RESPONSE_PREFIX` reply with
`error_code="EMPTY_RESPONSE"`.

Two properties matter. `_has_visible_output` treats a `function_call` as visible,
so a normal tool hop is never mistaken for silence (retrying would re-run the
tool). And the no-retry-after-streaming guard is now about *visible* output
rather than "yielded something" — a response carrying no content is invisible to
the caller, so re-running after one cannot duplicate anything the user can see.

**Honest scope:** this was found by code inspection, not by reproducing a live
failure, and it is **not** the cause measured in section 1 — that one is an
exception. It closes a provable path and makes any future silence diagnosable
(`WARNING … returned an empty turn`, `ERROR … returned no content on all N
attempts`, plus the labelled reply). If empties persist after deploy with no
marker and no log line, the cause is above the model layer.

## 6. Measuring it: `verify_router_health`

```bash
uv run python -m src.eval.verify_router_health --agent-id 6134089059699523584
uv run python -m src.eval.verify_router_health --agent-id <ID> --repeat 5 --threshold 0.05 --json
```

Seven prompts spanning the complexity range — the per-tier breakdown is what
turned "rare and intermittent" into "one tier, 50%", so a probe set that doesn't
span the bands is worse than none. Fresh session per turn (a reused session would
let one poisoned context explain later empties — see the caveat below, where that
choice cuts the other way) and the raw-SSE client, since the
SDK's array-only parser can't read a recycled engine's NDJSON (memory
`agent-engine-sse-parse-skew`) and a parse error miscounted as an empty stream
would corrupt the number being measured.

Reporting decisions:

* **Silent empties counted apart from labelled ones.** A throttled or
  labelled-empty turn still failed, but the user saw why. Folding them together
  would make the retry wrapper's contribution invisible.
* **Wilson interval on the rate.** 4/28 is not "14%" — it is 14% with a 95% CI of
  [5.7%, 31.5%]. At demo sample sizes the point estimate alone is false precision.
* **Skipped turns excluded but reported.** The first live run died at turn 18 on a
  `create_session` `KeyError: 'output'` and threw away the whole measurement.
  A session/transport failure is now recorded as `SKIPPED`, excluded from the
  rates (it says nothing about the router) and printed — counting it as an empty
  would blame the router for a control-plane blip, and dropping it silently would
  make a truncated run look complete.
* **Zero samples never passes.** No data is not a green light.

Exits non-zero above `--threshold` (default 5%), so it works as a gate.

## Caveats

- **The mechanism does not (yet) fully explain the 4 measured empties.** §4 proves
  the bug and the fix on a *mixed-tier* session, but `verify_router_health` opens a
  **fresh session per turn**, so those 28 turns had no earlier Gemini hop to leave
  an `adk-` id behind. What is established for the deployed run is correlation, not
  the causal chain: Cloud Logging shows four distinct
  `AnthropicError: 'tool_call_id'` events on engine `6134089059699523584` inside the
  probe window (22:24:27, 22:27:26, 22:40:34, 22:46:45), matching the four empties,
  and the failing turns show `events=1` — one tool call, then nothing, which is the
  shape of a death on the replay hop. Some *other* route to a missing id is
  therefore still unaccounted for on the deployed engine (a different ADK version is
  installed there than the pinned 2.6.3 read here). **The fix does not depend on
  which:** `restore_tool_call_ids` fills any absent id whatever removed it.
- **No after-measurement for *this* fix alone.** §4 is a local A/B. The deployed
  AFTER was taken only once the memory limit was also raised, so the two changes
  cannot be separated in the live numbers — what is established live is that the
  `tool_call_id` tracebacks stopped, and that they were not the thing keeping the
  streams empty (see [router-claude-tier-oom.md](router-claude-tier-oom.md)).
- **n=28 is small.** The BEFORE CI is [5.7%, 31.5%]. Proving the *after* rate is
  near zero needs a larger sweep than one `--repeat 4`.
- **Opus tier untested.** No probe scores ≥0.95, so only the sonnet band was
  exercised. Opus goes through the identical `LiteLlm` path, so the same change
  should cover it — inference, not measurement.
- **Fixing the ids may reveal further Claude-tier issues.** These turns have never
  successfully completed a multi-step tool sequence on this router; the
  `tool_call_id` KeyError is the first error, not provably the only one.
  **This is what happened** — see [router-claude-tier-oom.md](router-claude-tier-oom.md).
