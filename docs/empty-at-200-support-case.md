# Support case: intermittent empty responses on HTTP 200 from Agent Engine

**Prepared 2026-09-21.** Everything below is measured. Where a number is an estimate
or rests on a small sample, it says so.

---

## Summary

A deployed Agent Engine (Reasoning Engine) intermittently returns **HTTP 200 with a
zero-length response body**. The stream opens, yields events, and completes with no
text. There is no exception, no error payload, and no non-2xx status.

The rate is **episodic**: it sits at 0% for weeks, then runs at 8-14% for a week or
more, with no change on our side in between. Over the 40 days of retained data:
**85 empty responses in 1,710 monitored requests (5.0% overall)**, ranging from 0.0%
to 13.8% week to week.

We have eliminated six candidate causes on our side with controlled experiments. We
are asking for help identifying what produces the empty body server-side.

## Environment

| | |
| --- | --- |
| Project | `hybrid-vertex` |
| Region | `us-central1` |
| Engine (control) | `projects/hybrid-vertex/locations/us-central1/reasoningEngines/3639024497392091136` |
| Engine last updated | **2026-08-21T21:40:58Z** — unchanged throughout all observations below |
| Second engine | `…/reasoningEngines/4380288848559603712` (updated 2026-09-17), behaves identically |
| Runtime | ADK on the managed Agent Runtime, `AdkApp` with managed Session + Memory Bank services |
| Container | cpu 4 / **memory 16Gi** (not the 4Gi default), `min_instances` **4** |
| Backbone | `gemini-2.5-flash`, native Gemini path (not LiteLlm) |
| Client call | `agent_engines.get(...)` then `create_session()` + `stream_query()` |

## The symptom, precisely

- HTTP 200.
- The stream yields chunks; concatenating all `content.parts[].text` gives `""`.
- **Median latency 2.6s for an empty response vs 4.8s for a successful one**
  (n=34 empty, n=230 successful, pooled over two controlled experiments on
  2026-09-21). Empty responses come back *faster* than successful ones, so this does
  not look like a timeout or a truncation.
- No exception is raised client-side; nothing distinguishes the call from a success
  except the absent text.
- Cloud Trace: on previously investigated instances the enclosing `invoke_agent`
  span was missing, consistent with the worker dying mid-call. That signature was
  associated with an OOM at the 4Gi default, which we have since fixed (16Gi) — the
  current empties occur at 16Gi with `min_instances` 4.

## Measurement method

All rates come from one definition, applied identically everywhere:

```python
def is_infra_empty(response: str) -> bool:
    s = str(response).strip()
    return (not s) or s.startswith('{"error"')
```

In the experiments below **every** counted empty was a genuine zero-character
response; none were error-shaped. Requests use a fixed set of six domain prompts
(travel/expense assistant), one session per request unless stated otherwise.

## The rate is episodic, not a regression

Hourly sampling, 6 requests per batch, same engine throughout:

| week of | empty / requests | rate | 95% CI (Wilson) |
| --- | --- | --- | --- |
| 08-17 | 53/384 | 13.8% | [10.7%, 17.6%] |
| 08-24 | 4/432 | 0.9% | [0.4%, 2.4%] |
| 08-31 | 0/240 | 0.0% | [0.0%, 1.6%] |
| 09-07 | 0/270 | 0.0% | [0.0%, 1.4%] |
| 09-14 | 28/336 | 8.3% | [5.8%, 11.8%] |
| 09-21 | 0/48 | 0.0% | [0.0%, 7.4%] |

Three quiet weeks between two noisy ones, with no deployment, configuration or
client change between them. **We initially read this as a step change on 09-17 and
were wrong** — that comparison used a start date chosen after seeing the data. The
table above is the whole retained series.

## What we have ruled out

All experiments ran on 2026-09-21 against engine `3639…` (unchanged since 08-21),
with the same prompts and the same definition of empty.

| # | Candidate | Verdict | Evidence |
| --- | --- | --- | --- |
| 1 | The specific engine / its deployment | ruled out | an engine untouched since 2026-08-21 exhibits the full 0-14% range |
| 2 | A difference between our two engines | ruled out | 11.4% vs 11.4% (n=114 each), sampled interleaved on the same schedule |
| 3 | Client library version | ruled out | crossover design, below |
| 4 | Request spacing / cold start | ruled out | 0s gap 19.2% vs 60s gap 15.4% (n=52 each), intervals overlap |
| 5 | Session reuse | ruled out | fresh 10.0% vs reused 10.0% (n=80 each, across 20 independent sessions) |
| 6 | Conversation history length | ruled out | turns 1→4 within a session: 10% / 0% / 15% / 15% |

**#3 in detail** (google-adk 2.8.0 + google-genai 2.22.0 vs 2.9.1 + 2.24.0, both
against the same engine, 7 rounds of 6 requests):

| | block 1 (old ran first) | block 2 (new ran first) |
| --- | --- | --- |
| old client | 5/42 | 3/42 |
| new client | 0/42 | 4/42 |

The empties track **which arm ran first** (10.7% vs 3.6%), not the library version.

## The one signal we cannot explain

On the same engine, in the same week, with the same prompts and the same definition:

| origin | empty / requests | rate | 95% CI |
| --- | --- | --- | --- |
| Client on a developer workstation | 46/432 | **10.6%** | [8.1%, 13.9%] |
| Client on a GitHub-hosted runner | 0/48 | 0.0% | [0.0%, 7.4%] |

Intervals do not overlap. The only varying factor is where the request originates.
We note the CI arm is a single partial week (n=48) and that the same CI path
measured 13.8% and 8.3% in earlier weeks, so we offer this as a lead rather than a
conclusion.

## Questions

1. What server-side conditions cause the Agent Runtime to complete a `stream_query`
   with HTTP 200 and no content? Is there a code path that returns 200 with an empty
   body rather than surfacing an error?
2. Are there server-side logs or metrics for engine `3639024497392091136` that
   distinguish these calls from successful ones? We see nothing client-side.
3. The empty responses return **faster** than successful ones (2.6s vs 4.8s median).
   Does that narrow the phase in which the request is being abandoned?
4. Is there anything that would make the rate depend on the caller's network origin
   or region rather than on the engine?
5. Is the episodic multi-week pattern consistent with any known capacity, rollout or
   scheduling behaviour in `us-central1`?

## What we have already tried

- Raised container memory 4Gi → 16Gi (fixed an earlier, distinct OOM-driven case).
- Raised `min_instances` 1 → 4 (fixed an earlier container-recycle case).
- Bounded all list-returning tool payloads (fixed an earlier 429-driven case).
- Restored tool-call ids for the Anthropic path (fixed a router-specific case).
- Client-side retry on a silent turn, which converts some empties into slower
  successful answers but does not address the cause.

Each of those fixed a *different*, confirmed instance of the same symptom. The
residual rate documented here persists with all of them in place.
