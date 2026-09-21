# Empty-at-200 is still present, and none of the five causes explains it

*Measured 2026-09-18. This note adds no fix. It records a live rate, rules out the
documented causes and one attractive wrong answer, and states exactly what the next
measurement has to be — because the alternative is somebody re-deriving all of this
from two anecdotes.*

## How it surfaced

Two multi-turn discrimination runs against probe `4380…` each lost one conversation
to `stopped=empty_response`. Both failures happened to be on booking-lookup prompts,
which made a tidy story: `list_all_bookings` is the tool that caused **cause 2** (an
unbounded payload tripping a 429), so it looked like a recurrence.

**That story was wrong**, and it fell over as soon as the probe included controls:

| prompt | empty (n=5) | tools called |
| --- | --- | --- |
| `list all my recent bookings` (suspected) | **0/5** | `list_all_bookings` |
| `cancel a booking… cannot find the ID` (suspected) | 1/5 | `list_all_bookings` |
| `find flights SFO→JFK` (**control**) | 1/5 | — none — |
| `is a $450 dinner within policy` (**control**) | 2/5 | `check_expense_policy` |

The suspected prompt was the *cleanest* one, and a prompt that calls no tool at all
failed too. Two observations had produced a causal hypothesis that five each
destroyed. The cost of the controls was four extra minutes.

## What is actually happening

The rate is real, current, and affects both live coordinator engines:

| engine | source | empty |
| --- | --- | --- |
| `4380…` (ADK 2.9.1, redeployed 2026-09-17) | raw probe | 4/20 |
| `4380…` | `online_monitor --dry-run` | 1/6 |
| **`4380…` pooled** | | **5/26 = 19%**, 95% CI [9%, 38%] |
| `3639…` (older dependency set) | raw probe | 2/20 |
| `3639…` | `online_monitor --dry-run` | 0/6 |
| **`3639…` pooled** | | **2/26 = 8%**, 95% CI [2%, 24%] |

For context, the hourly `agent_online_eval/infra_empty_rate` series (which tracks
`3639…`) over the preceding 7 days: **median 0%, avg_24h 3.3%, max 16.7%, n=38.**
So this is a known intermittent level that the monitoring has been recording all
along — not a sudden new breakage.

## What is ruled out

All five documented causes, on their own signatures:

| cause | why not |
| --- | --- |
| 1 — replicas at `min_instances=1` | both engines run 4 |
| 2 — 429 from an unbounded tool payload | `list_all_bookings` is capped at 20 (`MAX_BOOKINGS_RETURNED`); the tool-free control failed too |
| 3 — OOM at 4Gi on a LiteLlm tier | both are 16Gi, both Gemini |
| 4 — `adk-*` tool-call id strip | Claude-tier only; these are `gemini-2.5-flash` |
| 5 — OOM at 4Gi on a Gemini-only engine | both are 16Gi; `verify_engine_config` reports 0 critical on both |

Also ruled out: **prompt or tool specificity** (see the table above).

## What is NOT established, and this is the point

`4380…` trends worse than `3639…` — 19% against 8% — and the two differ mainly by
the dependency set, since `4380…` was updated in place to ADK 2.9.1 on 2026-09-17.
That is an attractive conclusion and **the data does not support it**:

    Fisher exact p = 0.42, and the 95% intervals overlap heavily: [9%, 38%] vs [2%, 24%]

At n=26 per arm this cannot distinguish 19% from 8%. Separating them at 80% power
needs roughly **150 samples per arm**. Until that is run, "ADK 2.9.1 raised the empty
rate" is a hypothesis, not a finding, and nobody should roll back a dependency on it.

Note how cause 5 was actually cracked, for calibration: an engine A/B of **6-7/10 vs
2/10 vs 0/10** — differences large enough to read off ten samples. Nothing here is
that clean.

## The next measurement

1. **A properly powered A/B**, ~150 requests per engine, identical client code,
   interleaved rather than run in blocks so drift cannot masquerade as an engine
   difference. `src/eval/online_monitor.py --agent-id <ID> --samples N --dry-run` is
   the calibrated instrument — it already defines infra-empty
   (`is_infra_empty`) the same way the monitored series does. Do not hand-roll a
   probe for this; the first one here disagreed with the instrument (20% vs 16.7%)
   closely enough to be reassuring, but a hand-rolled definition of "empty" is one
   more thing to get wrong.
2. **Cheaper alternative that costs nothing:** the hourly monitor already tracks
   `3639…`. Pointing a second scheduled run at `4380…` accumulates the comparison in
   the background, and the two series can be compared after a week with no burst
   spend. This is the recommended route unless the answer is needed today.

## Why this note exists rather than a fix

Because the honest state is "a real ~8-19% rate with no established cause", and that
is worth writing down precisely. The failure mode this repo keeps hitting is a
plausible story adopted on two data points — and this investigation produced one
(the booking-tool theory) that was refuted within the hour by adding controls.

---

# 2026-09-21: six eliminations, and a retraction

*This section answers the "next measurement" above, eliminates six candidate causes
with controlled experiments, and **retracts a step-change claim made earlier the same
day**. The retraction is first because everything downstream of it was reasoned from
a window I chose badly.*

## RETRACTED: there was no regression on 09-17

Earlier on 2026-09-21 this investigation reported a 20x step change — "0/588 empties
before 09-17, 13/114 after, p ~ 1e-31" — and spent several hours bisecting for its
cause. **That comparison was an artifact of the window.** The "before" period was
2026-09-01 to 09-17, which happens to be a quiet stretch. Widened to the full
retained series, the rate is **episodic**, not stepped:

| week of | empty / requests | rate | 95% CI |
| --- | --- | --- | --- |
| 08-17 | 53/384 | **13.8%** | [10.7%, 17.6%] |
| 08-24 | 4/432 | 0.9% | [0.4%, 2.4%] |
| 08-31 | 0/240 | 0.0% | [0.0%, 1.6%] |
| 09-07 | 0/270 | 0.0% | [0.0%, 1.4%] |
| 09-14 | 28/336 | **8.3%** | [5.8%, 11.8%] |
| 09-21 | 0/48 | 0.0% | [0.0%, 7.4%] |

The rate oscillates between ~0% and ~14% on a multi-week cycle. 09-17 was the start
of one noisy episode, not the onset of anything. There was no cause to find, and
"what changed on 09-17" was the wrong question for about three hours of work.

Two things produced the error, both worth naming because neither is exotic:

1. **A window chosen after seeing the data.** The 48h `verify_monitors` default
   showed a high rate; reaching back for a "before" landed on 09-01 because that is
   where the zeros were. Picking the comparison period to make a contrast is how you
   manufacture one.
2. **A very small p-value read as proof.** `p ~ 1e-31` is not evidence against
   chance when the alternative is not chance but *selection*. The arithmetic was
   right and the conclusion was wrong.

The corrective is cheap and was skipped: plot the whole series before splitting it.

## What IS established

Six candidate causes were eliminated with controlled experiments, all against
coordinator engine `3639…`, which **has not been redeployed since 2026-08-21** and is
therefore a control rather than a variable. All used the monitored series' own
`is_infra_empty` definition and the same six `ONLINE_PROBE_PROMPTS`.

| candidate | result | how |
| --- | --- | --- |
| The served engine | ruled out | an engine untouched since August shows the full range, 0% to 14% |
| Engine-to-engine difference | ruled out | pinned 11.4% vs probe 11.4% (n=114 each), interleaved on the same hourly tick |
| Client library version (#139, adk 2.8.0→2.9.1, genai 2.22→2.24) | ruled out | crossover, below |
| Request spacing | ruled out | gap 0s 19.2% vs 60s 15.4% (n=52 each), intervals overlap |
| Session reuse | ruled out | fresh 10.0% vs reuse 10.0% (n=80 each, 20 independent sessions) |
| Conversation history length | ruled out | turns 1-4: 10% / 0% / 15% / 15%, flat |

### The crossover that saved the client hypothesis from being wrong

Old client vs new client, same engine, 7 rounds of 6 requests each:

| | block 1 (OLD ran first) | block 2 (NEW ran first) |
| --- | --- | --- |
| OLD | **5**/42 | 3/42 |
| NEW | 0/42 | **4**/42 |

Block 1 alone reads as a clean verdict: the old client is broken, the new one is
perfect. It reverses when the order flips. The empties follow **whichever arm ran
first** (10.7% vs 3.6%), not the client. A fixed A-then-B ordering manufactured the
effect; only running the mirror image exposed it.

### The session finding that did not survive its own confirmation

A 2x2 found fresh 1.9% vs reuse 32.7%, intervals separating — a 17x effect, the
first thing in this investigation whose confidence intervals came apart. It was
wrong. All 52 "reuse" requests shared **one** session, so the arm was n=1 dressed as
n=52. Re-run across 20 independent sessions: **10.0% vs 10.0%**, identical, with the
empties spread over 7 of 20 sessions rather than concentrated.

The tell was visible before the confirmation: the reuse rate *decayed* over the run
(50% → 28% → 19%), which fits "one session that was bad early" and not "reuse is
bad".

## The one live lead

Today's local probes and the CI monitor disagree, this week, on the same engine:

| | empty / requests | rate | 95% CI |
| --- | --- | --- | --- |
| Local probes (4 experiments, 2026-09-21) | 46/432 | **10.6%** | [8.1%, 13.9%] |
| CI hourly monitor (week of 09-21) | 0/48 | 0.0% | [0.0%, 7.4%] |

Intervals do not overlap. Same engine, same prompts, same definition of empty — the
difference is *where the client runs*. That is consistent with everything above:
nothing about the engine, the request pattern or the client library moves the rate,
and the one factor that does is the environment the call originates from.

Caveat in proportion: n=48 for the CI arm is one partial week, and CI itself measured
13.8% and 8.3% in earlier weeks. This is a lead, not a finding.

## What NOT to do next

Do not run a seventh client-side experiment. Six factors have been eliminated and
the pooled rate has not moved off ~10% in any of them; the marginal one is worth
little. The remaining explanations are environmental or platform-side, and the
useful next artifact is a support case with the evidence above rather than another
probe — see `docs/empty-at-200-support-case.md`.
