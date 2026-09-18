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
