# Empty-at-200: a live 8–19% failure rate with no established cause

**Analysis report — 2026-09-18**
Engines examined: `4380288848559603712` (demo probe), `3639024497392091136` (pinned coordinator)
Companion note: [`docs/notes/empty-at-200-still-present-2026-09.md`](notes/empty-at-200-still-present-2026-09.md)

---

## Bottom line

Between **8% and 19%** of requests to our deployed coordinator engines return **HTTP
200 with zero characters of text**. The rate is live, reproducible, and **not
explained by any of the five causes this project has previously diagnosed and
fixed**.

This report does not contain a fix. It contains a measurement, the elimination of
six candidate explanations (five documented, one invented during the
investigation), and a precise statement of the experiment needed to go further. The
most important content is the section titled *What this does not establish*.

**No action is recommended today beyond one free change.** In particular, do not
roll back the recent dependency upgrade on the strength of this data — see
*The tempting conclusion*.

---

## 1. What "empty-at-200" means

A client sends a request to a deployed agent. The HTTP status is **200 OK**. The
event stream opens, yields events, and closes. The agent's visible answer is the
empty string.

There is no exception, no error field, no traceback, and nothing in the response
that distinguishes it from a successful call. Downstream, an automated evaluator
scores the empty string as a *bad answer* rather than recording it as a failed
request — which is why the symptom matters disproportionately: **an infrastructure
failure is silently reported as a quality problem.**

This project has hit the symptom five times before, from five different causes, and
maintains a triage guide for exactly that reason
([`docs/notes/empty-at-200-field-guide.md`](notes/empty-at-200-field-guide.md)).

---

## 2. How it surfaced

While validating a new multi-turn evaluation harness, two separate runs each lost
one conversation to an empty stream. Both failures happened to occur on prompts
about **looking up bookings**.

That was a suggestive coincidence: one of the five known causes is an oversized tool
payload from the booking-list tool overwhelming the model call. A recurrence was the
natural first hypothesis.

---

## 3. Method

Two instruments, deliberately:

1. **A direct probe** — four fixed prompts, five fresh sessions each, against one
   engine at a time. Two prompts were the suspected booking lookups; **two were
   controls**, one of which calls no tool at all.
2. **`src/eval/online_monitor.py --dry-run`** — the project's existing, calibrated
   instrument. It defines "empty" via `is_infra_empty()`, the same definition used
   by the hourly monitoring series, so its numbers are directly comparable to seven
   days of recorded history.

Using both matters. A hand-rolled definition of "empty" is one more thing to get
wrong, and the agreement between the two (20% vs 16.7% on the same engine) is what
makes the number trustworthy rather than an artefact of my own probe.

---

## 4. The first hypothesis was wrong

The booking-tool theory did not survive contact with the controls:

| prompt | empty (n=5) | tools invoked |
| --- | --- | --- |
| "list all my recent bookings" *(suspected)* | **0/5** | `list_all_bookings` |
| "cancel a booking… cannot find the ID" *(suspected)* | 1/5 | `list_all_bookings` |
| "find flights SFO→JFK" *(**control**)* | 1/5 | **none** |
| "is a $450 client dinner within policy" *(**control**)* | 2/5 | `check_expense_policy` |

The suspected prompt was the **cleanest of the four**, and a prompt that invokes no
tool at all failed as often as the suspects.

Two observations had generated a confident causal story; five observations each
destroyed it. The controls cost roughly four extra minutes. This is the single most
transferable lesson in the report.

---

## 5. What the rate actually is

| engine | direct probe | `online_monitor` | pooled | 95% CI |
| --- | --- | --- | --- | --- |
| `4380…` — demo probe, redeployed 2026-09-17 with ADK 2.9.1 | 4/20 | 1/6 | **5/26 = 19%** | [9%, 38%] |
| `3639…` — pinned coordinator, older dependency set | 2/20 | 0/6 | **2/26 = 8%** | [2%, 24%] |

**Historical context, from monitoring that was already running:** the hourly
`agent_online_eval/infra_empty_rate` series (which tracks `3639…`) over the
preceding seven days — median **0%**, 24-hour average **3.3%**, maximum **16.7%**,
n=38 points.

So this is a **known, intermittent level that the monitoring has been recording all
along**, not a sudden new breakage. Nobody had looked at the series and asked what
it implied.

---

## 6. What is ruled out

Each of the five previously diagnosed causes, eliminated on its own documented
signature:

| # | Cause | Why it is not this |
| --- | --- | --- |
| 1 | Replica recycling at `min_instances=1` | both engines run `min_instances=4` |
| 2 | HTTP 429 from an unbounded tool payload | the booking-list tool is capped at 20 records; the **tool-free control failed too** |
| 3 | Out-of-memory kill at the 4Gi default, Claude tier | both engines are 16Gi, both run Gemini |
| 4 | Tool-call-ID stripping on Anthropic models | Claude-only; these engines run `gemini-2.5-flash` |
| 5 | Out-of-memory kill at 4Gi, Gemini-only engine | both are 16Gi; config verification reports **0 critical** on both |

Also ruled out, from §4: **prompt specificity** and **tool specificity**.

The conclusion is that the project's catalogue describes **five *explained* causes,
not five causes**. The field guide has been amended so its table is not read as
exhaustive.

---

## 7. The tempting conclusion — and why the data does not support it

`4380…` (19%) looks worse than `3639…` (8%). The two engines are configured
identically except that `4380…` was redeployed on 2026-09-17 with a newer dependency
set (ADK 2.9.1). That is a clean, plausible story: *the upgrade raised the empty
rate.*

The data does not carry it:

```
Fisher exact test:  p = 0.42
95% intervals:      [9%, 38%]  vs  [2%, 24%]     — heavily overlapping
```

At 26 samples per arm, an apparent 19%-vs-8% difference is entirely consistent with
chance. Distinguishing those two rates at 80% statistical power requires roughly
**150 samples per arm**.

For calibration on what a real signal looks like here: cause 5 was identified by an
engine A/B reading **6–7/10 vs 2/10 vs 0/10** — a difference visible in ten samples.
Nothing in the present data is remotely that clean.

**Therefore: the dependency upgrade is neither implicated nor exonerated.** It is
untested. Rolling it back on this evidence would be acting on noise, and would also
discard a verified upgrade.

---

## 8. Recommendations

| | action | cost | rationale |
| --- | --- | --- | --- |
| **1** | Point a second scheduled monitoring run at `4380…` | **free** | the hourly monitor already tracks `3639…`; adding the second engine accumulates a properly powered comparison in the background, with no burst spend. Compare the two series in a week. |
| 2 | If the answer is needed sooner: an **interleaved** A/B, ~150 requests per engine, via `online_monitor` | ~300 requests | interleaved, not run in blocks, so time-of-day drift cannot masquerade as an engine difference. Use the calibrated instrument, not a bespoke probe. |
| 3 | Treat the ~8–19% rate as a known operating condition until (1) or (2) resolves it | — | evaluation pipelines already partition empty responses out of quality scores; that separation is what keeps the rate from corrupting quality metrics. |

Recommendation **1** is the one to take. It answers the question for free, on a
timescale of days, and the question is not urgent: the rate is long-standing, the
monitoring records it, and the eval surfaces already exclude it from quality means.

---

## 9. Reproducing this

```bash
# The calibrated instrument. --dry-run computes without publishing.
uv run python -m src.eval.online_monitor --agent-id <ENGINE_ID> --samples 10 --dry-run

# Seven days of recorded history for the same measurement:
uv run python -m src.eval.verify_monitors --format json --hours 168
```

Read `infra_empty_rate` in the output. Note that a single run of ten samples has a
95% interval roughly ±20 percentage points wide — which is the reason this report
stops where it does.

---

## Appendix: why this report has no fix in it

The honest state is *"a real 8–19% rate, cause unknown, five explanations
eliminated"*. Writing that down precisely is more useful than a sixth plausible
story.

The failure mode this project repeatedly encounters is a confident explanation built
on two data points. This investigation produced exactly such a story — the
booking-tool theory in §4 — and refuted it within the hour by adding two control
prompts. The same discipline is why §7 stops short of blaming the dependency
upgrade, despite it being the obvious candidate and the difference looking large.
