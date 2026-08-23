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

The lite and flash tier engines were re-pinned to `gemini-2.5-flash-lite` /
`gemini-2.5-flash` (in-place `--update`, same engine ids) and the medium band re-run:

| medium band | lite | flash | decisive | win rate | 95% CI | p |
| --- | --- | --- | --- | --- | --- | --- |
| Gemini-3 | `3.1-flash-lite` | `3.5-flash` | 19 (18–1) | 94.7% | 75–99% | 0.0001 |
| **Gemini-2.5 (what the router serves)** | `2.5-flash-lite` | `2.5-flash` | 16 (14–2) | **87.5%** | 64–97% | **0.0042** |

**Replicated.** Weaker at 2.5 (3 ties instead of 0, and the CI reaches down to 64%)
but unambiguous, and 0 cases dropped in both runs. The high band was **not** re-run —
its conclusion is "change nothing", which is the safe default either way.

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

* **The high-band result crosses vendors AND generations** — sonnet is
  `claude-sonnet-4-6` (which the router does serve), but pro was measured as
  `gemini-3.1-pro-preview` while the router serves `gemini-2.5-pro`. "Sonnet wins"
  therefore does **not** establish "these prompts need less power". The *decision*
  it supports is confound-free (do not lower `COMPLEXITY_HIGH`; the alternative
  measured worse), but the interpretation is not — so the 7 disputed cases were
  **not** relabelled, even though the pre-registered rule's `BASELINE_BETTER` branch
  nominally called for it. It must not be cited as evidence that 0.80 is *optimal*.
  De-confounding needs a same-vendor, same-generation contrast.
* **Re-pinning the tier engines changed shared infrastructure.** `lite_agent` and
  `flash_agent` now run Gemini-2.5, matching the router; `pro_agent` and
  `sonnet_agent` are still on the Gemini-3 defaults. Anything driving those engines
  (`cross_model_experiment`, future boundary runs) now measures a mixed set.
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
