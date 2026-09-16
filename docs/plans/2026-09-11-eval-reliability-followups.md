# Eval reliability follow-ups — parked 2026-09-11

Three recommendations coming out of [`docs/notes/eval-reliability-audit.md`](../notes/eval-reliability-audit.md)
(PR #116). **None is started.** Parked deliberately to pick up other work; this file
exists so they are revisited rather than rediscovered.

Ordered by importance. Only the first is a safety claim.

---

## 1. Make the safety test able to fail

**Status: not started. Needs an owner decision before any code.**

`src/armor/config.py` blocks on four regexes; `tests/test_eval_gate_safety.py`
`ADVERSARIAL_PROMPTS` holds five prompts, one per pattern. The test asserts four
regexes match the five strings they were written for, and it runs inside the
**required** suite. Measured 2026-09-10: 5/5 corpus prompts blocked, **1/10 held-out
injections** blocked, 0 false positives on benign.

`"Ignore all previous instructions…"` → blocked. `"Ignore all prior instructions…"`
→ passes. One synonym.

**Why it is parked and not just done:** widening the corpus turns a required,
always-green check **red**, and the honest fix is probably not "add more regexes" —
a blocklist loses this game indefinitely. The real answer is to assert on the layer
that does the work (Model Armor templates, or the ADK plugin), which today has no
test at all. Flipping a required check and choosing that design is an owner call.

**Scope limit, restated so this is not read as alarmism:** a server-side layer is
always active (`armor_layers()`), so the *system* is not as exposed as the guardrail
alone. The defect is in the **eval** — the green tick carries no information about
injection resistance.

---

## 2. Point the scored path at the holdout that already exists

**Status: not started. Scoped 2026-09-11 — it is two jobs, not one.**

`holdout.py` + `dataset_integrity.py` exist and are CI-enforced, but they guard
`src/eval/evalsets/*` while `multi_agent_batch_eval` scores
`src/eval/agent_eval_configs.py` and publishes *that* to `agent_eval/*`.
`_select_cases` never consults `holdout`. For `travel_agent`, **0 of 3** held-out
probes appear in the scored set.

| agent | scored | GEPA train | contaminated | train left if decontaminated |
| --- | --- | --- | --- | --- |
| coordinator | 61 | 21 | 8 | **13** |
| travel | 10 | 7 | 2 | **5** |
| expense | 10 | 7 | 4 | **3** |

### 2a — plumbing (~1 working session, one PR, low risk)

Extend `holdout` / `dataset_integrity` to cover the scored collection, make
`_select_cases` holdout-aware, add a test that fails when the **scored** set gets
contaminated. All the patterns exist; this points them at the right collection.

**Important limitation to state in that PR:** 2a does **not** make the GEPA numbers
trustworthy. It makes the untrustworthiness visible and bounded — the test records
the current 40% / 20% / 13% contamination and fails on any increase.

### 2b — content (blocked, needs a domain owner)

Decontaminating expense leaves **3** training cases; travel leaves 5. You cannot
GEPA-optimize a prompt against 3 prompts. A real fix needs ~10–20 new eval cases per
agent so both a train set and a holdout exist. That is domain judgement about what a
travel-and-expense agent should be graded on — mass-generating it produces plausible
prompts of unverified value, which is how the sets got thin in the first place.

---

## 3. Decide whether the eval suite is a smoke test or a gate

**Status: not started. A decision, not a task.**

It is currently sized as a smoke test and read as a quality gate. A genuinely
breached agent (true mean 2.9 against a 3.0 floor) returns `inconclusive` at n=8, 20
and 50 — never `breached` — and `inconclusive` exits 0.

Two legitimate answers; picking one beats drifting:

* **Smoke test.** Cheap and fast — but label it. Say "detects large regressions only"
  on the dashboard and in the gate summary, and add gold cases at 3 and 4 so
  calibration at least covers the boundary it defends.
* **Real gate.** ~200 coordinator cases (measured: that is what resolves 2.9 vs 3.0 at
  sd 0.5; ~800 at sd 1.0), gold sets for the three uncalibrated judges, and a
  multi-turn surface to replace the quarantined `simulated_eval`.

---

## Smaller, unblocked

* **Three uncovered tools.** `cancel_booking`, `get_booking_details`,
  `list_all_bookings` are exercised by no eval case — the same three the 2026-08-21
  prompt audit added to the instruction *because* they had no coverage. The
  instruction was fixed; the evalset was not. A few cases closes it.
* **20 unlabelled gold cases.** `uv run python -m src.eval.annotate --annotator a2`.
  Cheap, and calibration is currently blind where it counts.
* **Router outcome metric.** Publish one, or state on the dashboard that the router
  series measure classifier and cost only — a routing collapse currently *improves*
  `cost_savings_pct` (93.09% → 99.59%) and fires nothing.

## Also parked (unrelated to evals)

* [`2026-09-09-gemini-enterprise-publication.md`](./2026-09-09-gemini-enterprise-publication.md)
  — blocked on a GE license.
* Console screenshots still come from `wortz-project`, not `hybrid-vertex`; the
  README provenance block says so honestly. Re-capturing needs console access.
