# The `router_boundaries` factor was inert (and the fix)

**Date:** 2026-08-06 · **Context:** first full DOE screening run
(`doe_runs/screening-20260806`, 9 pipeline jobs).

## Symptom

Every one of the 9 design points returned **byte-identical** routing/cost
metrics — `routing_accuracy=1.0`, `savings_pct=63.2`, `routed_cost_usd=0.17867715`
— even though 4 of the 9 (`dp05`–`dp08`) carried the `aggressive_savings`
level of the `router_boundaries` factor. If that factor did anything, those four
should have shown a different cost.

## Investigation (what was and wasn't the cause)

The override **did reach** the component — this was *not* a plumbing bug:

1. **Dispatch confirmed.** `manifest.json` shows `dp05`–`dp08` with
   `COMPLEXITY_LOW=0.45, MEDIUM_SPLIT=0.60, COMPLEXITY_HIGH=0.75, HIGH_SPLIT=0.90`
   in `factor_env`; the rest carry the `0.30/0.45/0.60/0.80` defaults.
2. **The env→config→complexity chain works.** `eval_pipeline._FACTOR_ENV`
   (compile-time `os.environ` read) → `set_env_variable` on every task →
   `src.config` (import-time env read) → `src.router.complexity.THRESHOLDS`.
   Proven locally: reloading `src.router.complexity` with the aggressive env
   shifts `THRESHOLDS` to `[0.45, 0.75]` and reclassifies gap-zone scores
   (0.35 medium→low, 0.65 high→medium).

Two real reasons the metrics didn't move:

1. **Half the factor was structurally ignored.** The eval scorers keyed off the
   coarse 3-tier `.level` (low/medium/high), which depends only on
   `COMPLEXITY_LOW/HIGH`. `MEDIUM_SPLIT` and `HIGH_SPLIT` are consumed *only* by
   `score_to_model_tier()` — called by the live 5-tier router agent, which the
   DOE never deploys. And `run_cost_efficiency_eval`'s old `MODEL_MAP` mapped
   `.level` to just LITE/PRO/OPUS (`medium → PRO`; the `medium_low`/`medium_high`
   → FLASH/SONNET keys were never produced by `classify_complexity`). So the cost
   model was a coarse 3-bucket function that couldn't express flash/sonnet at all.
2. **The classifier scores dodge the remaining knobs.** With `temperature=0` and
   well-separated eval prompts, the classifier emits quantized scores
   `{0.10, 0.15, 0.20, 0.45, 0.75, 0.85, 0.90}`. The shift only opens
   reclassification gap zones `[0.30, 0.45)` and `[0.60, 0.75)`; **no observed
   score lands in either**, and the aggressive boundaries (0.45, 0.75) coincide
   exactly with emitted scores, which the strict `<` keeps on the same side.

Net: `router_boundaries` was a **placebo channel** for this eval — regardless of
whether the override reached.

## Fix (#1): route the cost eval through the real 5-tier router

`src/eval/complexity_metrics.py` — `run_cost_efficiency_eval` now selects the
model via `score_to_model_tier(result.score)` and a 5-tier `TIER_MODEL`
(lite/flash/sonnet/pro/opus) instead of the 3-bucket `.level → MODEL_MAP`. Now
**all four** boundary knobs (`COMPLEXITY_LOW/HIGH` + `MEDIUM_SPLIT/HIGH_SPLIT`)
drive model selection and therefore cost, and the cost model can express the
flash and sonnet tiers. Per-case output also records the chosen `tier`.

Projected against the observed scores, baseline vs aggressive now **diverges on
5 of 12 cases** (three sonnet→flash, two opus→pro) → a real savings delta:

| boundaries | tier distribution |
|---|---|
| baseline `[0.30,0.45,0.60,0.80]` | lite×5, sonnet×3, opus×3, pro×1 |
| aggressive `[0.45,0.60,0.75,0.90]` | lite×5, flash×3, pro×3, opus×1 |

Locked in by `tests/test_complexity_boundaries.py::test_cost_eval_responds_to_boundary_factor`.

## Still-open follow-ups (not done here)

The score-clustering (reason 2) still limits sensitivity. To fully exercise the
factor: use boundaries that fall *between* score clusters, broaden
`ROUTER_EVAL_CASES` with near-boundary prompts (or raise classifier
temperature), or drop `router_boundaries`/`eval_fidelity` from the *coordinator*
screen entirely — the coordinator doesn't route, so neither moves its quality
metrics. See [the DOE framework note](./doe-framework.md).
