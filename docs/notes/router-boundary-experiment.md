# The router boundary experiment — settling accuracy vs savings with data

**Date:** 2026-08-23 · **Code:** `src/eval/router_boundary_experiment.py` ·
**Tests:** `tests/test_router_boundary_experiment.py`

## The problem

PR #85 lit the `agent_router/*` series for the first time and found two monitored
metrics apparently at war:

```
routing_accuracy_pct   50.0%  (20/40)   floor 80%   -> BREACHED
cost_savings_pct       94.3%            floor 50%   -> fine
```

The classifier was not broken. Its scores separate the three bands with **zero
overlap** — low prompts score exactly `0.10`, medium exactly `0.40`, high
`0.75`–`0.90` — and the cut-points simply sliced between those clusters by a hair:

| cut-point | value | effect |
| --- | --- | --- |
| `COMPLEXITY_LOW` | 0.44 | every `0.40` "medium" prompt fell **below** it → ran on **lite** |
| `COMPLEXITY_HIGH` | 0.80 | every `0.75` "high" prompt fell **below** it → ran on **sonnet** |

Those boundaries were DOE-tuned for savings and delivered 94.3%. The obvious
reading was that the two metrics encode opposing goals and someone had to make a
product call.

**That reading was wrong, and the reason matters:** `routing_accuracy_pct` scores
conformance to a complexity *label*. It says nothing about whether the answer was
any good. Nobody had ever measured whether the cheap tier's answers were worse.

## The instrument

Two paired side-by-side comparisons, cheap tier vs would-be tier, on every prompt
labelled for that band. Reuses `src/eval/pairwise_eval.py` (`cases` is injectable,
so no change to it was needed) with its shipped rigour: **flip debiasing** to
cancel judge position bias and **4 samples** per case, majority-voted. Significance
via `stats.win_rate_significance` — exact two-sided sign test plus a Wilson CI.

`src/eval/cross_model_experiment.py` was the obvious candidate and is the wrong
instrument: it reads only `summary_metrics` (`/AVERAGE`), so it yields means with
**no per-case scores** and cannot carry a confidence interval.

**Case selection.** All 19 medium-labelled and 20 high-labelled prompts, not just
the disputed subset — pooling `ROUTER_EVAL_CASES` with `TIER_EVAL_CASES` and
deduping. This was forced by power: the sign test needs **11/13** on the disputed
medium cases and a perfect **7/7** on the disputed high ones, versus a reachable
**15/19** and **15/20** on the full bands.

**The decision rule was pre-registered** in the module docstring before the run —
`CANDIDATE_BETTER` / `BASELINE_BETTER` / `NO_DIFFERENCE` / `INCONCLUSIVE`, with
`NO_DIFFERENCE` deliberately hard to reach (it requires the CI to *exclude* a
meaningful effect in both directions, because "we found no difference" is not
"there is no difference"). `verdict()` implements that table and nothing else.

## The result

Both bands came back significant, **in opposite directions**. Zero cases were
dropped for empty/error responses in either band.

| band | comparison | decisive | win rate (bigger model) | 95% CI | p | verdict |
| --- | --- | --- | --- | --- | --- | --- |
| medium | lite → flash | 19 (18–1) | **94.7%** | 75.4–99.1% | **0.0001** | `CANDIDATE_BETTER` |
| high | sonnet → pro | 14 (2–12), 30% ties | **14.3%** | 4.0–39.9% | **0.0129** | `BASELINE_BETTER` |

### Replication on the models the router actually serves

The first run drove the standalone tier engines, which were on the repo's **Gemini-3**
defaults — but the router's tiers are deliberately pinned to **Gemini-2.5**. So the
18–1 result was real and about the *wrong model pair*. Shipping on it would have
repeated, in a new form, the dilution mistake below: acting on a number measured
against a system that is not the one being changed.

Every tier engine the experiment drives was re-pinned to the router's model (in-place
`--update`, same engine ids) and **both bands re-run**. `sonnet_agent` needed no
change — it already served `claude-sonnet-4-6`.

**Medium — replicated.**

| medium band | lite | flash | decisive | win rate for flash | 95% CI | p |
| --- | --- | --- | --- | --- | --- | --- |
| Gemini-3 | `3.1-flash-lite` | `3.5-flash` | 19 (18–1) | 94.7% | 75–99% | 0.0001 |
| **Gemini-2.5 (served)** | `2.5-flash-lite` | `2.5-flash` | 16 (14–2) | **87.5%** | 64–97% | **0.0042** |

Weaker at 2.5 (3 ties instead of 0, CI reaching down to 64%) but unambiguous.

**High — replicated, and *more* strongly.** The working hypothesis was that a Gemini
*preview* pro losing 12–2 might be a preview-model artefact, and that the shipped
`gemini-2.5-pro` could win — which would have taken accuracy to ~100% at lower cost
(pro is ~35% cheaper per case than sonnet). **It did not.** Swapping to the served
model roughly doubled sonnet's margin:

| high band | sonnet | pro | decisive | win rate for pro | 95% CI | p |
| --- | --- | --- | --- | --- | --- | --- |
| first run | `claude-sonnet-4-6` | `3.1-pro-preview` | 14 (2–12) | 14.3% | 4–40% | 0.0129 |
| **Gemini-2.5 (served)** | `claude-sonnet-4-6` | `2.5-pro` | 18 (1–17) | **5.6%** | 1–26% | **0.0001** |

0 cases dropped in any of the four runs.

So `COMPLEXITY_HIGH` stays at 0.80 — the same decision as before, but no longer
resting on a model the router doesn't serve. **The hypothesis that motivated the
re-run was refuted, which is the point of running it rather than assuming.**

### The high band is not homogeneous — and the router splits it

A pooled win-rate says nothing about a subset, and the high band is exactly where
that bites: the router sends prompts scoring `< COMPLEXITY_HIGH` to **sonnet** and
the rest to **pro** — the tier that just lost 17–1. `per_case` originally held only
`{prompt, choice}`, so answering this needed another paid run; it now carries each
prompt's classifier score and routed tier (`annotate_per_case`), and
`subband_split` scores each side separately.

| split at `COMPLEXITY_HIGH=0.80` | routes to | sonnet–pro | win rate for pro | p | significant |
| --- | --- | --- | --- | --- | --- |
| below (score 0.75) | sonnet | 11–1 | 8% | 0.0063 | yes |
| **at/above (0.85, 0.90)** | **pro** | **6–0** | **0%** | **0.0312** | **yes** |

**Sonnet beats pro on both sides of the cut**, including the sub-band the router
currently routes to pro. Tier distribution over the 20 prompts: sonnet 11, pro 8,
flash 1 (one "high"-labelled prompt scores 0.45 — a genuine classifier
disagreement, not a boundary artefact).

Three things temper this before anyone acts on it:

1. **n=6 with zero losses is the thinnest significance obtainable.** 6–0 gives
   p=0.0312; a single loss would make it 5–1, p=0.219, not significant. It clears
   the pre-registered bar, but only just.
2. **Raising `COMPLEXITY_HIGH` costs money**, unlike the medium fix. Sonnet is
   **$0.0081/case** against pro's **$0.00525** — moving the upper sub-band to sonnet
   *reduces* `cost_savings_pct`.
3. **It would empty the pro tier.** With opus already unreachable (`HIGH_SPLIT`=0.95
   above the top observed score), the 5-tier router would populate three.

Note what does *not* enter this trade any more: `classifier_accuracy_pct` is now
invariant to the boundaries, so it neither improves nor degrades. That is the
re-scope working — the decision is being made on quality and cost alone.

**The accuracy metric was half right and half wrong.**

* **Medium:** the metric was right. `COMPLEXITY_LOW=0.44` was costing real quality —
  flash beat lite on 18 of 19 prompts.
* **High:** the metric was wrong. The tier those prompts *already* get beats the one
  the label wants, 12–2.

## What changed

**`COMPLEXITY_LOW`: 0.44 → 0.25** (`src/config.py`). 0.25 is the **midpoint** of the
classifier's two observed clusters (0.10 and 0.40), so it is maximally robust to
drift in either direction and does not coincide with an emitted score — which the
router's strict `<` would mis-handle.

**`COMPLEXITY_HIGH`: unchanged at 0.80**, on the same evidence. Lowering it would
route the high band to pro and make answers measurably worse.

Re-measured over the same 40 cases:

| metric | before | after | floor |
| --- | --- | --- | --- |
| `routing_accuracy_pct` | 50.0% | **82.5%** | 80% ✅ |
| `cost_savings_pct` | 94.3% | **94.0%** | 50% ✅ |
| `classifier_latency_ms` | 554.8 | 597.5 | 8000 ✅ |

Tier distribution moved from `lite 27 / sonnet 8 / pro 5` to
`lite 14 / flash 13 / sonnet 8 / pro 5`.

**The two metrics were never in conflict.** 32.5pp of accuracy cost 0.3pp of
savings. No threshold was moved to achieve it.

### `routing_accuracy_pct` → `classifier_accuracy_pct` (2026-08-24)

That 50% → 82.5% jump is itself the evidence that the metric was broken. **The
classifier never changed** — only `COMPLEXITY_LOW` did. The metric graded the
classifier by bucketing its score with `complexity._score_to_level`, which uses the
*tunable routing cut-points*, so it was scoring cut-point placement and reporting it
as classifier skill. Two live consequences: 33/40 = 82.5% sat **one misroute from a
false page** against its 80% floor, and every future boundary change — including one
made *because* a paired experiment said so — perturbed an alerting series.

Two questions had been conflated, and they now have separate homes:

| question | instrument |
| --- | --- |
| Does the classifier score prompts into the right band? | `complexity.score_to_reference_band` — fixed thirds of 0-1, wired to nothing tunable |
| Does the router send a band to the best tier? | the paired experiments here, pinned in `tests/test_routing_constraints.py` |

The series is renamed because the name was part of the bug. **Points are not
comparable across the changeover.** Measured after:

```
COMPLEXITY_LOW=0.25  ->  classifier_accuracy_pct 100.0%  (40/40)
COMPLEXITY_LOW=0.44  ->  classifier_accuracy_pct 100.0%  (40/40)   # invariant
```

Under the old code those two differ by 32.5pp. 100% is the *true* reading — the
classifier separates the bands with zero overlap — and it is still a working alarm:
a thinking `CLASSIFIER_MODEL` returns empty text, every prompt takes the low-score
fallback, and every medium/high case goes wrong.

Equal thirds rather than the observed cluster midpoints on purpose: midpoints fitted
to today's eval set would need re-deriving whenever the score distribution shifted,
reintroducing the same coupling somewhere new. `tests/test_reference_bands.py` pins
the invariance directly.

## The finding worth remembering

The DOE screening that chose `COMPLEXITY_LOW=0.44` recorded the change as costing
"a ~0.04 quality dip". That number was a **dilution artefact**. The screening
scored a rubric *mean over a mixed dataset*, so damage confined to 13 medium-band
prompts was averaged away by 27 unaffected ones. The paired design on just the
affected prompts found flash winning 18–1.

A dataset-mean A/B can report "no meaningful quality cost" for a change that is
badly hurting the subpopulation it touches. **When a change is targeted, measure
it on the target** — and pair the comparison, which is far more sensitive than
diffing two independent means.

## Caveats

* **The high-band result still crosses vendors** (Claude sonnet vs Gemini pro). The
  re-run fixed the *generation* mismatch, not the vendor one, so "sonnet wins" does
  **not** establish "these prompts need less power" — it may say more about the two
  specific models. The *decision* it supports is sound (do not lower
  `COMPLEXITY_HIGH`; the alternative measured worse on both pro models tested), but
  it must not be cited as evidence that 0.80 is *optimal*. That is why the 7
  disputed cases were **not** relabelled, even though the pre-registered rule's
  `BASELINE_BETTER` branch nominally called for it.
* **Re-pinning the tier engines changed shared infrastructure.** `lite_agent`,
  `flash_agent` and `pro_agent` now run Gemini-2.5 rather than the repo's Gemini-3
  defaults, so anything driving them (`cross_model_experiment`, future boundary runs)
  measures the router's models rather than `src/config.py`'s.
* **`routing_accuracy_pct` has only 2.5pp of headroom, and the shortfall is
  correct.** 33/40 = 82.5% against an 80% floor: **one more misroute breaches it.**
  The 7 that are "wrong" are the 0.75-scoring prompts, now measured twice, on two
  different pro models, as being better off exactly where the router puts them. The
  metric is provably penalising correct routing, so the fix when it does breach is to
  re-scope it — accuracy over prompts where tier choice demonstrably changes quality
  — **not** to lower the floor. See the recommendation in `quality_alerts.py`.
* **Gemini-only judge**, as everywhere in this repo — a Claude judge 404s on the
  `publishers/google` path.
* **Answer quality only.** Latency and per-token cost are not in this measurement;
  `cost_savings_pct` covers the cost side separately.
* The residual 17.5% accuracy shortfall is exactly the 7 disputed high cases. It is
  not a routing defect. If a future case mix pushes it into a breach, the fix is to
  re-scope the metric (accuracy over prompts where tier choice demonstrably changes
  quality), **not** to lower the floor.

## Re-running it

```bash
uv run python -m src.eval.router_boundary_experiment --dry-run   # plan + counts, spends nothing
uv run python -m src.eval.router_boundary_experiment             # ~20 min, 4 engines, 156 judge calls
uv run python -m src.eval.router_boundary_experiment --band medium
```

Needs the four tier engines live (`LITE_/FLASH_/SONNET_/PRO_ENGINE_ID`); it
preflights them and exits 1 rather than failing deep inside a paid run.
