# The Claude tiers were OOM-killed: 4Gi is not enough for a LiteLlm engine

**Status:** root-caused and **fixed live**. Router `6134089059699523584` went from
**8/8 empty** on Claude-tier probes to **0/8**, measured 2026-08-21 on the same
probe set minutes apart with no code change between the two runs — only the
container memory limit. Confirmed on the standing instrument:
`verify_router_health --repeat 4` went **24/28 → 28/28 full (14.3% → 0.0% silent
empty)**, and 56 traffic queries ran with 0 errors.

This is the "further Claude-tier issue" that
[router-empty-stream-retry.md](router-empty-stream-retry.md) predicted in its last
caveat. That note's `tool_call_id` fix was real and its tracebacks are gone from
the logs, but it was **not** the dominant cause: after it shipped, the Claude
tiers still failed **100% of the time**, including on prompts that call no tools
at all.

## 1. What the failure looked like

Every prompt scoring into the 0.60–0.80 **sonnet** band returned HTTP 200, one
event, and **zero characters**. Deterministic, not intermittent:

```
  no-tool, complex     tier=sonnet/0.75  events=1 toolcalls=0 chars=0   8.9s  EMPTY
  one-tool, complex    tier=sonnet/0.75  events=1 toolcalls=0 chars=0   8.9s  EMPTY
```

The Gemini tiers (lite / flash / pro) were clean throughout, and the identical
code path — the real `router_agent` forced to sonnet — answered **locally** in
34.6s with 3,749 characters. So it was not the tier dispatcher, not the tool loop,
and not the model.

## 2. Why it was invisible

Three things had to be added before the runtime would say anything at all.

**Cloud Trace was the first real evidence.** A failing sonnet trace contained
`router.route`, the classifier's `generate_content`, `coordinator.memory_preload`
and a `GET` 200 — but **no `call_llm` span and no enclosing `invoke_agent` /
`invoke_workflow`**. An exception closes its spans and they export; a truncated
trace that loses the *enclosing* span means the process died with those spans
still open. **Only a SIGKILL does that.**

**Dispatch logging then named the tier and the moment.**
`TierRoutingLlm.generate_content_async` now logs every outcome — the tier it
dispatched to, any exception (`logger.exception`, so a re-raise ADK would swallow
is recorded first), and a turn that ends with zero responses. Without it a Claude
failure produced *no log line whatsoever*.

**A `finally`-free `except Exception` is not enough on its own**, which is worth
knowing: `asyncio.CancelledError` and `GeneratorExit` are `BaseException`, so a
cancelled turn would also slip past. In this case the process really was killed,
so nothing ran at all — but the logs are what proved which.

The decisive log window (PID 23, one sonnet request):

```
00:20:21.373  Tier dispatch -> vertex_ai/claude-sonnet-4-6 (litellm_loaded=True, stream=False)
00:20:21.455  LiteLLM completion() model= claude-sonnet-4-6; provider = vertex_ai
00:20:27.063  <UserWarning: FeatureName.PLUGGABLE_AUTH is enabled>   ← unprefixed: a NEW worker booting
```

The call starts, then ~5.6s later a **fresh worker process boots**. No traceback,
no "Shutting down", no error of any kind. PID 23 never logs again.

## 3. Root cause

The engine's `resource_limits` was `{}` — the Agent Runtime default of **cpu 4 /
memory 4Gi**. The container runs several worker processes (PIDs 14, 15, 18, 19,
21, 22, 23 were all observed serving), and each worker that touches a Claude tier
loads litellm on top of ADK, the three MCP toolsets, and the model clients.
Measured locally: litellm adds **~140MB resident** to the router process
(168.4MB → 308.1MB) and peaks at **502.6MB (+334MB)** during a full Claude turn.
Multiply by the worker count and a Claude turn pushes the container over 4Gi; the
kernel SIGKILLs a worker mid-invocation and the managed runtime returns the
already-open 200 with nothing in it.

**Empty-at-200 is the signature of a killed worker**, not of a model that said
nothing — see §5.

## 4. The fix

Raise the limit. `resource_limits` is settable on an **in-place update**, so this
needed no repackage and no recreate:

```
resource_limits: {'cpu': '4', 'memory': '16Gi'}   # was {} → default 4Gi
```

Result on the identical probe set, ~2 minutes later:

```
  lite?    tier=lite/0.0     events=2 tools=0 chars=77      3.7s  FULL
  lite?    tier=lite/0.1     events=2 tools=0 chars=69      3.4s  FULL
  flash?   tier=lite/0.1     events=4 tools=1 chars=64      5.0s  FULL
  flash?   tier=lite/0.1     events=4 tools=1 chars=42      4.2s  FULL
  sonnet?  tier=sonnet/0.75  events=4 tools=1 chars=7463   48.5s  FULL
  sonnet?  tier=sonnet/0.75  events=2 tools=0 chars=11010  69.5s  FULL
  pro?     tier=pro/0.9      events=2 tools=0 chars=466     7.3s  FULL
  opus?    tier=pro/0.9      events=2 tools=0 chars=368     7.7s  FULL

  0/8 EMPTY
```

And on the same instrument that produced the BEFORE — `verify_router_health
--agent-id 6134089059699523584 --repeat 4`, the tier-labelled sweep whose 4/28
found the sonnet band in the first place:

| | BEFORE (2026-08-20) | AFTER (2026-08-21) |
| --- | --- | --- |
| full | 24/28 (85.7%) | **28/28 (100.0%)** |
| silent empty | 4 (14.3%) CI [5.7%, 31.5%] | **0 (0.0%) CI [0.0%, 12.1%]** |
| lite / flash / pro | 20/20 clean | 20/20 clean |
| **high (sonnet band)** | **4/8 empty** | **8/8 full** |
| latency | p50 7.7s / p95 18.4s | p50 4.0s / p95 30.3s |

`generate_traffic --router-only --count 2` independently ran **56 router queries
with 0 errors** (high=16, low=26, medium=14), every one returning real text.

The p95 rise is the point, not a regression: the sonnet turns that used to die at
~9s now run to completion in ~30s and return 3.7-11k characters. An empty stream
was always the *fast* outcome.

To keep it from silently reverting, `deploy_agents._auto_memory()` derives the
limit from the agent's own backbones: `_model_ids()` collects every reachable
model id (bare string, `.model` on a wrapper like `RetryingLlm`, or the router's
`TierRoutingLlm._tier_models`), and if any of them `needs_litellm()` the deploy
config gets `resource_limits = {"cpu": "4", "memory": "16Gi"}`. A Gemini-only
engine keeps the platform default and is completely unaffected. `--memory 32Gi`
overrides both.

This is deliberately *derived*, not a flag to remember: the router already has
one mandatory-flag trap (the tier env overrides, without which a plain
`--update` regresses the tiers to Gemini-3) and adding a second would be a
worse bug than the one being fixed.

## 5. What this retroactively explains

**Memory `coordinator-outage-is-runtime-not-model` (2026-08-13)** recorded that a
fresh-deploy empty-stream outage "crashed all fresh **LiteLlm-wrapped** engines"
while a native-Gemini coordinator was healthy — and left native-fix vs
platform-recovered unresolved. The distinguishing factor in that outage was
exactly the one measured here: **LiteLlm-wrapped means litellm resident in every
worker at 4Gi.** That is now the leading explanation for the older outage too.
It is inference from a shared signature, not a re-measurement of a 2026-08-13
engine, so it is stated as such.

It also explains why **prewarming litellm at startup did not help**. Moving the
import out of the request path was sound reasoning about a *different* failure
mode (a blocking 2s import inside a live request), and the `litellm_loaded=True`
in the dispatch log proves the prewarm does run on the server. But it does not
reduce the peak, and by loading litellm into *every* worker rather than only
those that serve a Claude turn it slightly **raised** the floor. It is kept
because the reasoning still holds once there is headroom, and because
`litellm_loaded=` in the dispatch line is what ruled the import out as the cause.

## 6. Caveats

- **16Gi is headroom, not a measurement.** There is no per-container memory
  utilization metric for Agent Engine — the only memory metric exposed is
  `reasoning_engine/memory/allocation_time`, which is billing, not usage. So the
  actual peak was never observed directly; 16Gi was chosen to be comfortably
  clear of it and confirmed by the outcome. A tighter limit (8Gi) may well be
  enough and would be cheaper; it was not tested.
- **The kill is inferred, not read from a kernel log.** Agent Engine surfaces no
  OOM-killer message. The evidence is the SIGKILL signature (trace missing its
  enclosing span, no traceback, a fresh worker booting 5.6s into the call) plus
  the fact that changing *only* the memory limit fixed it.
- **The opus tier is still untested end-to-end.** No probe scores ≥0.95, so the
  ≥0.95 band was never reached — the "opus?" row above classified to pro. Opus
  goes through the identical LiteLlm path as sonnet, so the same fix covers it by
  inference, not by measurement.
- **n=28 still bounds the residual loosely.** 0/28 is 0% with a 95% CI of
  **[0.0%, 12.1%]** — it rules out the old 14.3% rate but does not prove the true
  rate is below a few percent. A larger sweep is needed for that claim.
- **Cost.** 16Gi is billed via `reasoning_engine/memory/allocation_time` and
  `min_instances=4` keeps four containers warm. This is a real, ongoing cost
  increase on the router, not a free change.
