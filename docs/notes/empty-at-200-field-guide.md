# Empty-at-200: which one is it?

*Field guide, 2026-08-21. A lookup layer over four completed investigations — it
adds no new evidence, it just says which note you want.*

**Empty-at-200** is a *symptom*, not a cause: the engine returns HTTP 200, the
stream yields events, and the text is zero characters. There is no traceback and no
error in the response. Four separate things have produced it on this project —
three confirmed causes plus one contributing factor whose mechanism was never
established — and **they are indistinguishable from the client**. Each needed a
different lever, so "we already fixed the empty streams" is never a complete
statement. Ask *which one*.

## The four

| # | Cause | Distinguishing signature | Fix | Note |
| --- | --- | --- | --- | --- |
| 1 | Replica recycling / cold replicas at `min_instances=1` — **contributing factor, mechanism never proven** | many distinct worker PIDs and a fresh `service.instance.id` per request, across **all** tiers; trace loses its enclosing span (but see the warning below — that alone proves nothing) | `--min-instances 4`, which moved the rate | [coordinator-router-learnings.md](./coordinator-router-learnings.md) (superseded claim) + [router-empty-responses-quota.md](./router-empty-responses-quota.md) (falsification) |
| 2 | HTTP 429 `RESOURCE_EXHAUSTED` from an unbounded tool payload | `generate_content` span with `error.type=ClientError`, `input_tokens=0`, `finish_reasons=[]`; 215 of 219 project 429s attributed to engine `6134…`; router 17.2K input tokens/hop vs coordinator 1.85K | cap the MCP payload + retry/label 429s in `TierRoutingLlm` | [router-empty-responses-quota.md](./router-empty-responses-quota.md) |
| 3 | OOM `SIGKILL` at the default 4Gi on a LiteLlm/Claude tier | missing enclosing span **plus** a fresh worker booting ~5.6s *into* the LiteLLM call, no traceback; litellm is +140MB resident / +334MB peak per worker | `resource_limits` 16Gi (now derived by `deploy_agents._auto_memory()`) | [router-claude-tier-oom.md](./router-claude-tier-oom.md) |
| 4 | ADK strips the `adk-*` tool-call ids Anthropic pairs results by | Claude tier **only**, **multi-step** turns only, **mixed-tier** session only; `AnthropicError: 'tool_call_id'` | `restore_tool_call_ids()` in `RetryingLlm` | [router-empty-stream-retry.md](./router-empty-stream-retry.md) |

## A missing enclosing span does NOT prove a container kill

This inference is the single biggest trap here, and we fell into it once. The
argument looks airtight: an exception closes its spans and they export, so a trace
that lost its enclosing `invoke_workflow`/`invoke_agent` span means the process
died with those spans still open — only a kill does that.

**It was falsified for the Gemini-tier empties.** The same zero-character result
reproduces **locally, in-process, outside the managed runtime entirely**
(`elapsed=17.38s chars=0`) — no container, nothing to kill. So a truncated trace is
consistent with a kill but does not establish one, and the `min_instances=1`
root-cause claim built on it did not survive.

Treat the span shape as a **hint that needs a second, independent signal**:

| corroborating signal | what it establishes |
| --- | --- |
| a fresh worker booting **mid-call** (~5.6s into the LiteLLM call), no traceback, PID never logs again | a real kill — cause 3 |
| the fix moving the number (8/8 empty → 0/8 on 16Gi) | the kill was memory pressure |
| reproduces in-process with no runtime at all | **not** a kill; look at the model call itself — cause 2 or 4 |

## Telling 1 from 3 — both look like "the worker died"

| | 1 — recycled at the scale floor | 3 — OOM-killed |
| --- | --- | --- |
| trigger | platform scaling with `min_instances=1` | kernel OOM-killer at 4Gi |
| tiers affected | all of them, Gemini included | **LiteLlm-backed only** (Claude tiers) |
| extra tell | fresh replica *per request*, before work starts | fresh worker boots **mid-call**, ~5.6s in |
| lever | `--min-instances 4` | `resource_limits` `{"cpu":"4","memory":"16Gi"}` |
| confidence | **contributing factor only** — raising the floor moved the rate, but the mechanism was never proven and the span-shape argument for it was falsified | **established** — corroborated by the mid-call worker boot and by the fix |

If the empties follow the Claude tiers and the container has no explicit
`resource_limits`, it is 3. If they hit every tier and the replica identity changes
on every request, 1 is worth ruling out — but do not stop there, because 2 produced
exactly that picture too.

## Triage order

1. **Measure first, on a warm engine.**
   `uv run python -m src.eval.verify_router_health --agent-id <ID> --repeat 8`
   gives per-tier full/empty rates with a Wilson interval over 7 tier-spanning
   probes. **Do not probe immediately after a deploy** — that measures warm-up, not
   the build. On 2026-08-21 the first post-deploy probe read 2/28 empty (FAIL) and
   the warmed re-run read 56/56 FULL (PASS) with nothing changed in between.
2. **Which tiers?** Claude-only → 3 or 4. Every tier → 1 or 2.
3. **Check `resource_limits`.** Empty `{}` on a LiteLlm-backed engine is cause 3
   before you look at anything else.
4. **Check the 429 count** —
   `serviceruntime.googleapis.com/api/request_count`, `response_code="429"`, grouped
   by `credential_id`. Non-zero against the engine is cause 2; then look for an
   unbounded list-returning MCP tool feeding it.
5. **Check cause 4's three preconditions together** — Claude tier *and* a multi-step
   (tool-using) turn *and* a mixed-tier session. Any one missing and it reproduces
   clean, which is exactly why it hid for so long.

## Measured trajectory — read the provenance, not the curve

These numbers come from **different probe sets** and are not one time series. Do
not plot them together.

| when | measurement | probe set |
| --- | --- | --- |
| pre-fix | ~40% of turns empty | steady traffic, quota investigation |
| after 1 + 2 | ~14% empty | same, [router-empty-stream-retry.md](./router-empty-stream-retry.md) |
| after 1 + 2 | 2/20 → 17/24 full | raw-SSE load curve, 4 prompts × 3 rounds, lite+flash |
| cause 3 window | 8/8 empty → **0/8** | Claude-tier probes, before/after 16Gi |
| cause 4 window | sonnet band 4/8 empty | tier-spanning probes, lite/flash/pro 20/20 clean |
| 2026-08-21 | **56/56 FULL, 0.0%** | `verify_router_health --repeat 8`, post-ADK-2.7.1 |

## Why the older notes look contradictory

Two sentences written in the **same commit** (`70feca1`) can be quoted against each
other:

- [router-empty-responses-quota.md](./router-empty-responses-quota.md) falsifies
  "managed-runtime worker churn / container kills" — measured **after**
  `min_instances` was already 4 on both engines, so it is falsifying churn as the
  cause of the empties that *remained*.
- [coordinator-router-learnings.md](./coordinator-router-learnings.md) says the
  Gemini-tier empties *were* containers hard-killed at `min_instances=1` — a claim
  from the *earlier* window, resting on the span-shape inference above, which was
  later falsified. Raising the floor to 4 did move the rate; the stated mechanism
  is what didn't hold.

Neither sentence says which window it belongs to, which is what lets them be quoted
against each other. Both have since been scoped in place, and this note is the layer
that keeps them straight. The reconciling sentence was always there, at
[router-empty-stream-retry.md](./router-empty-stream-retry.md): *"`min_instances=4`
and the tool-payload cap were real fixes for real causes … but they left this
untouched — it was never a capacity or quota problem."*

Related: [online-infra-empty-and-baseline-alerts.md](./online-infra-empty-and-baseline-alerts.md)
partitions empty-at-200 out of the online quality mean and counts it as
`agent_online_eval/infra_empty_rate`, so an infra empty can no longer masquerade as
a bad answer.
