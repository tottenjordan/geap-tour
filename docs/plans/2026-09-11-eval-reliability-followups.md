# Eval reliability follow-ups — parked 2026-09-11

Three recommendations coming out of [`docs/notes/eval-reliability-audit.md`](../notes/eval-reliability-audit.md)
(PR #116). Parked deliberately to pick up other work; this file exists so they are
revisited rather than rediscovered.

Ordered by importance. Only the first is a safety claim.

> **Re-evaluated 2026-09-17.** Every item below was re-measured rather than re-read,
> and three had drifted: item 2's table was stale and missing the router, item 3's
> multi-turn premise was wrong (`simulated_eval` is fixed), and "three uncovered
> tools" was never accurate for the scored collection. **2a is now done.** Items 1,
> 2b and 3 remain open; the two other smaller items are re-confirmed still open.
> Corrections are marked inline rather than silently rewritten, so the drift itself
> stays visible.

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
that does the work. Flipping a required check and choosing that design is an owner
call.

**Updated 2026-09-17 — two facts here went stale; the recommendation narrows.**

* The ADK plugin is no longer untested. `tests/test_armor_observable_plugin.py`
  covers it — but only *structurally* (a screening failure and a genuine block emit
  different metrics; `block_on_screening_failure` still defaults closed). Nothing
  measures whether it stops an actual injection.
* More importantly, **the plugin is not the live layer on the default backbone
  any more.** `AGENT_MODEL` moved to `gemini-2.5-flash`, and `armor_layers()` gates
  the plugin on `not server_side_armor_enabled(model)` — so today the default
  coordinator reports `server_side=True, plugin=False`. The layer actually doing the
  work is the **Model Armor templates**, and those have no behavioural test.

So the recommendation is now specific: point the measurement at the templates on the
regional-Gemini path. Still an owner decision, because it still means deciding what a
required check is allowed to assert.

**Scope limit, restated so this is not read as alarmism:** a server-side layer is
always active (`armor_layers()`), so the *system* is not as exposed as the guardrail
alone. The defect is in the **eval** — the green tick carries no information about
injection resistance.

---

## 2. Point the scored path at the holdout that already exists

**Status: 2a DONE 2026-09-17. 2b still blocked.**

`holdout.py` + `dataset_integrity.py` exist and are CI-enforced, but they guard
`src/eval/evalsets/*` while `multi_agent_batch_eval` scores
`src/eval/agent_eval_configs.py` and publishes *that* to `agent_eval/*`.

Re-measured 2026-09-17 (the 2026-09-11 table was stale and had no router row):

| agent | scored | GEPA train | contaminated | train left | holdout | **holdout ∩ scored** |
| --- | --- | --- | --- | --- | --- | --- |
| coordinator | 61 | 19 | 6 | 13 | 5 | **3** |
| travel | 10 | 7 | 1 | 6 | 3 | **0** |
| expense | 10 | 7 | 4 | 3 | 3 | **1** |
| router | 40 | 21 | 1 | 20 | 5 | **0** |

The headline the first pass missed: **9 of 16 held-out probes never reach the scored
path at all.** The generalization probes were reserved, CI-enforced, and then not
used by the thing that publishes the number.

### 2a — plumbing (DONE)

`holdout.scored_prompts` / `scored_contamination` / `is_holdout_case` join the scored
collection to the holdout; `_select_cases` puts held-out cases first **when limiting**
(so the CI gate's `--limit 8` spends its budget on prompts GEPA never saw) while
leaving an unlimited run byte-identical; `multi_agent_batch_eval` prints the
contamination line next to every score; and
`tests/test_eval_dataset_integrity.py:TestTheScoredCollectionIsMeasured` pins the
numbers above as bounds — contamination may not rise, holdout coverage may not fall.
Both directions mutation-checked.

**What 2a does not do:** it does not make the GEPA numbers trustworthy. It makes the
untrustworthiness visible, bounded and regression-guarded. For `travel_agent` and
`router_agent` the holdout-first sort currently selects **zero** cases, because they
have no held-out probes in the scored set — that is 2b's problem, surfaced rather
than hidden.

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
  sd 0.5; ~800 at sd 1.0), gold sets for the three uncalibrated judges, and a working
  multi-turn surface.

**Updated 2026-09-17 — the multi-turn premise changed.** This item used to call for "a
multi-turn surface to replace the quarantined `simulated_eval`". That is no longer the
task: PR #138 fixed `simulated_eval` (three defects, all ours — the quarantine's claim
of an upstream SDK bug was wrong), and `eval_gate.yaml` no longer says otherwise.

What a live run now shows is a *different* problem, and a more interesting one. The
harness works; the coordinator **fails** it — `0.33 / 0.00 / 0.00` against a 0.60
floor. The raters are not blind (an inference-only probe found a real trajectory:
`CALL:expense_mcp_get_user_expenses -> RESP -> text`), but the conversation collapses
to **one turn** despite `--max-turns 3`, and multi-turn raters have nothing to grade
across a single turn.

So the multi-turn surface exists and reports. The open question is why it only ever
gets one turn — simulator, config, or the agent ending early. That is the follow-up,
and it is unblocked. See `docs/notes/adk-2.7.1-dependency-refresh.md`
("Working is not passing").

---

## Smaller, unblocked

* **Three uncovered tools — HALF DONE, and this item was mis-stated.**
  `cancel_booking`, `get_booking_details` and `list_all_bookings` have carried
  `expected_tool` entries in `agent_eval_configs.py` since `97045a0` (2026-08-22) —
  *before* this doc was written, so "exercised by no eval case" was never accurate
  for the scored collection. They remain absent from `src/eval/evalsets/*.json`,
  which is the collection GEPA and the holdout machinery read. Verified 2026-09-17.
* **20 unlabelled gold cases.** `uv run python -m src.eval.annotate --annotator a2`.
  Cheap, and calibration is currently blind where it counts. Re-confirmed 2026-09-17:
  `--status` reports `a2: 0/52 scored`.
* **Router outcome metric.** Still open, re-confirmed 2026-09-17:
  `quality_alerts.ROUTER_MONITORED_METRICS` holds only `classifier_accuracy_pct`,
  `cost_savings_pct` and `classifier_latency_ms`. Publish an outcome metric, or state
  on the dashboard that the router series measure classifier and cost only — a
  routing collapse currently *improves* `cost_savings_pct` (93.09% → 99.59%) and
  fires nothing.

## Also parked (unrelated to evals)

* [`2026-09-09-gemini-enterprise-publication.md`](./2026-09-09-gemini-enterprise-publication.md)
  — blocked on a GE license.
* Console screenshots still come from `wortz-project`, not `hybrid-vertex`; the
  README provenance block says so honestly. Re-capturing needs console access.
