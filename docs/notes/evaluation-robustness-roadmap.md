# Evaluation robustness — assessment + roadmap

**Question:** how do we make the eval system more *robust* — i.e. produce scores
we can trust to be reproducible, unbiased, and to actually catch regressions?

This note is a grounded audit of the current eval surface (offline batch,
standalone judges, online monitor, simulated, pairwise, alerting, datasets) and a
prioritized set of interventions. Every claim is backed by a `file:line`.

## What exists today (one-paragraph map)

Quality is scored three ways: (1) the Vertex Gen AI Evaluation Service batch eval
with 6 managed `RubricMetric`s (`agent_eval_configs.py:541-548`), (2) two custom
standalone judges — `policy_judge.py`, `tool_use_judge.py` — that overwrite the
SDK-broken rubrics before publish, and (3) a client-side `online_monitor.py` over
sampled live traffic. Pairwise SxS (`pairwise_eval.py`) and a user-simulator
multi-turn eval (`simulated_eval.py`) are separate. Scores bridge to Cloud
Monitoring (`agent_eval/*`, `agent_online_eval/*`, `agent_router/*`) with static
alert floors (`quality_alerts.py:112-152`).

## Robustness gaps (grounded)

### G1 — Single-judge monoculture, non-deterministic, no retries
- **Every** LLM judge is one call to `gemini-2.5-flash`: `policy_judge.py:35`,
  `tool_use_judge.py:42`, `online_monitor.py:52`, `pairwise_eval.py:39`; the
  user-simulator is the same model (`config.py:188`). The 6 batch rubrics use the
  single managed autorater. No cross-model panel anywhere.
- **No temperature is pinned** — all four judges call bare
  `client.models.generate_content(model=..., contents=prompt)` with no config
  (`policy_judge.py:134`, `tool_use_judge.py:141`, `online_monitor.py:269`,
  `pairwise_eval.py:202`). Scores are not reproducible run-to-run.
- **No retry/flakiness handling** on judge calls; an empty/garbled verdict just
  fails the `Score: N` regex and is silently dropped from the mean
  (`policy_judge.py:47-52,91-98`). The only retry in the stack is for *agent*
  inference, not the judge (`_sdk_patches.py`).
- **Consequence:** high-variance scores with a systematic autorater bias no second
  rater can catch; a flaky judge silently shrinks the sample.

### G2 — Train/eval contamination + demo-scale datasets
- The GEPA optimization prompts and the evaluation prompts are the **same set**:
  coordinator 17/17, router 21/21, travel 10/10, expense 10/10 overlap between
  `src/agents/*/‗eval_set.evalset.json` (GEPA train) and `src/eval/evalsets/*`
  (eval). Sampler configs declare only a `train_eval_set`, no holdout
  (`src/optimize/*_sampler_config.json`). **We optimize and grade on the same
  prompts → scores measure memorization, not generalization.**
- Largest single scored set is **49** coordinator cases
  (`batch_eval.py:49`), self-described "~50 curated … demo-scale" vs Google's
  ≥1000 guidance (`batch_eval.py:44-47`). Per-agent JSON sets are 10–27.
- Adversarial coverage is thin: **8** graded adversarial cases
  (`batch_eval.py:283-347`); multi-turn/memory is thin (34 sim scenarios, 3
  Memory-Bank conversations, 1 recall probe); **no long-context stress cases**.
- No dataset versioning/checksums/provenance; cases are hand-curated literals.

### G3 — No statistical rigor (can't separate signal from noise)
- Pass/fail is a bare mean-vs-threshold compare (`simulated_eval.py:174-178`,
  `multi_agent_batch_eval.py:183`, `verify_monitors.py:142-146`). No confidence
  intervals, no minimum-n gating, no significance test.
- Pairwise win-rate is a raw proportion with no significance test on the rate
  (`pairwise_eval.py:121-139`) — a 55% win rate over ~49 cases is reported as a
  win with no error bar.
- `verify_monitors` reports avg/min/max/p50/p90 but attaches no dispersion/CI and
  treats an empty bucket as `status=empty`, not a sample-size warning
  (`verify_monitors.py:149-170,217`).

### G4 — Static alert thresholds, no baselining
- All floors/ceilings are hand-picked literals with documented "headroom for
  variance" (`quality_alerts.py:112-152` + comment `:122-133`). No rolling
  baseline, control chart, z-score, or regression-vs-last-known-good. A slow drift
  that stays above 3.0 is invisible; normal variance near the floor false-alarms.

### G5 — Coverage/representativeness holes in the live surfaces
- Online monitor samples by **fixed stride, not random** (`online_monitor.py:148`)
  and the live path drives a hard-coded **6-prompt** probe set
  (`online_monitor.py:244-251`) — not representative of real traffic.
- The online monitor has **no empty-response guard** (unlike the three offline
  judges' `_is_error_response`, e.g. `policy_judge.py:101-104`), so it *conflates*
  "model gave a bad answer" with "infra returned empty at HTTP 200" (the exact
  effect seen in [online-quality-monitor.md](./online-quality-monitor.md): 39%
  empties dragged helpfulness to 2.87). Good that it *catches* empties — bad that
  the signal doesn't say *which* problem it is.
- Cross-session recall is a **substring match**, not a judge
  (`verify_cross_session_recall.py:163-164`) — brittle to paraphrase.
- Simulated (multi-turn) eval runs **only manually** (`eval_vertex.yaml` is
  `workflow_dispatch`), so multi-turn regressions never gate a PR.

### G6 — Correctness nits that undermine trust
- `multi_agent_batch_eval` CLI `--threshold` default is **0.5** (help says "1-5",
  `:353-354`) but the function default is **3.0** (`:232`) and thresholds are
  normalized `/5.0` (`:183`) — the CLI default effectively passes everything
  (0.5/5 = 0.10). The advisory CI gate only escapes this by passing `3.0`
  explicitly.
- Scenario/turn counts disagree across call sites (10 vs 5 scenarios, 5 vs 3
  turns: `simulated_eval.py:61-63,238` vs `run_all_evals.py:122-124`).

### G7 — No faithfulness check (claimed action vs executed tool) — ✅ addressed
- Every quality judge scored only `(prompt, final-response-text)` via
  `client.evals.run_inference`, which returns text but **no trajectory**, so
  nothing caught a **hallucinated action** — a reply claiming *"I booked FL001"*
  when no `book_flight` tool ran. **Addressed** by the grounded
  `tool_faithfulness` evaluator (see
  [tool-call-faithfulness.md](./tool-call-faithfulness.md)), which scores the
  response against the real `stream_query` trajectory. **Open risk:** its
  coordinator-level accuracy depends on the client stream surfacing nested
  sub-agent MCP calls (Branch A) — spike-gated and not yet confirmed live.

## Prioritized interventions

### P0 — cheap, high-trust (do first) — ✅ implemented
> Shipped in the eval-robustness-P0 change: shared deterministic+retry judge
> client (`src/eval/judge_client.py`), held-out eval split with a contamination
> guard (`src/eval/holdout.py`, `src/eval/dataset_integrity.py`,
> `tests/test_eval_dataset_integrity.py`), and the CLI threshold-default fix.
> P1/P2 remain open.

1. **Pin judge determinism + add retries.** Give every judge call a
   `GenerateContentConfig(temperature=0)` and a bounded retry/backoff on empty or
   error responses. One shared helper reused by all four judges. Removes run-to-run
   noise (G1) and stops silent sample shrink.
2. **Carve a held-out eval split.** Freeze an eval-only set that shares **zero
   prompts** with the GEPA `train_eval_set`; optimize on train, grade on holdout.
   Single biggest *validity* fix (G2). Add an automated overlap check
   (`tests/`) that fails if train∩eval ≠ ∅.
3. **Fix G6 nits.** Make the CLI `--threshold` default 3.0 (match the function +
   the "1-5" help), and unify scenario/turn counts to one constant.

### P1 — medium effort, big robustness
4. **Judge panel / self-consistency + report agreement.** ✅ **implemented**
   (`src/eval/judge_panel.py`, `tests/test_judge_panel.py`). Shipped as option
   (a) — a cross-generation Gemini panel (`gemini-2.5-flash` + `gemini-3.5-flash`,
   `DEFAULT_PANEL_MODELS`), each pinned to `temperature=0` via the P0 `judge_client`
   (self-consistency of one temp=0 judge would be degenerate, so the panel spans
   *models*, not samples). **Gemini-only by necessity:** the genai `generateContent`
   path `judge_client` uses only reaches Google-published models — a Claude/partner
   judge resolves to `publishers/google` and 404s (verified live 2026-08-18), so
   diversity spans Gemini *generations*, not vendors. Gemini-3.5 is judged on the
   *global* endpoint (`judge_client.resolve_judge_location`, mirroring
   `config.resolve_model`'s family split; a regional client 404s). Per item the verdict is
   the **median** (robust to one contrarian judge); the batch reports **inter-rater
   reliability** as **Krippendorff's alpha (interval)** plus mean per-item spread.
   Wired as an opt-in path into `policy_judge.run_policy_compliance_eval`
   (`panel=True` / injectable `judges=`); the aggregation core is pure and
   unit-tested with fakes (no GCP). Generic `score_pairs_with_panel` is reusable by
   the other pointwise judges. **Now also wired into the online monitor**
   (`online_monitor.score_interaction_panel` + `score_and_publish(judges=…)`,
   `run_online_monitor(panel=True)`, CLI `--panel`): live `agent_online_eval/*`
   rubric scores can be the panel **median** instead of the single autorater, and
   the summary prints per-rubric Krippendorff alpha + mean spread so a
   low-agreement online panel is visible. This closes the "single managed
   autorater … online_monitor.py" gap flagged in P0; faithfulness stays
   single-judge for now (grounded-trajectory judge; panel is future work).
5. **Confidence intervals + sample-size floors.** ✅ **implemented**
   (`src/eval/stats.py`, `tests/test_stats.py`). One shared stats module — a
   percentile `bootstrap_mean_ci`, an `is_low_confidence` / `confidence_label`
   sample floor (`MIN_SAMPLES = 8`), `wilson_ci` for proportions, and
   `binomial_two_sided_p` / `win_rate_significance` for the pairwise sign test.
   Wired into all four consumers: `verify_monitors` (per-metric `ci` +
   `low_confidence`), `multi_agent_batch_eval._annotate_low_confidence` (tags every
   metric graded over too few items), `online_monitor` (per-rubric `ci` +
   `low_confidence`), and `pairwise_eval` (a `significance` block, so a majority
   over a handful of cases can no longer read as a verdict). Renderers print
   `⚠ low_confidence` rather than hiding it. `wilson_ci` is also what
   `verify_router_health` and `sweep_empty_rate` report empty rates with — the one
   implementation, not a copy.
6. **Human-label calibration set.** ✅ **implemented** (`src/eval/calibration.py`,
   `src/eval/data/policy_calibration_gold.json`, `tests/test_calibration.py`). A
   32-case gold set of `(prompt, response, human_score)` policy-compliance triples;
   `score_judge_vs_gold` / `score_panel_vs_gold` run the single judge or the P1.4
   panel over the *frozen* responses (so calibration needs **no deployed engine**)
   and report judge-vs-human `mae`, signed `bias` (lenient/strict), `within_tolerance`,
   and `pearson` — so autorater drift/bias is measurable, not assumed (G1). The
   `python -m src.eval.calibration [--panel]` CLI prints the report and exits
   non-zero below a floor (a drift alarm). **Honest limit:** the seed labels are
   **author-curated single-annotator**, not independent multi-annotator human
   annotation — a directional probe, not a validated gold standard (see the file's
   `provenance` field).

   **2026-08-22 — the gate was failing, and the judge was not the problem.**
   Baseline: 68.8% within tolerance (floor 70% → FAIL), MAE 0.231, bias **-0.206**,
   Pearson r 0.571. The bias said the judge was systematically stricter than the
   humans, which read as an over-strict rubric. It was the opposite: **v1's 5/5
   reference responses cited policy the system does not implement** — a $200
   entertainment limit (real: $150), city-specific lodging caps for New York and
   Austin (real: flat $400), a "$500 domestic flight approval" tier (real: flat
   $200 transport), a *gifts* category (none exists), per-person meal scaling
   (real: flat $75), and travel-class rules (none exist). Two cases also scored a
   **refusal to submit** an over-limit expense 5/5, contradicting the
   coordinator's own spec ("Do not refuse to submit… flag it for review").
   The judge, correctly grounded in `mock_db.POLICY_LIMITS`, marked those down —
   and scored the good and bad member of each pair *identically* (0.2/0.2), which
   is why Pearson r was so low. **Calibrating against v1 would have tuned the
   judge to accept hallucinated policy.** Gold set corrected to v2; the judge and
   rubric were untouched at that point and the gate went to **96.9%, r 0.941**.
   `tests/test_rubric_grounding.py` now fails the build if any 5/5 reference cites
   a dollar figure that is not a real limit or prompt-derived.

   Cheap generalisation: an eval reference standard is code and rots like code.
   This one was never validated against the system it grades.

   **2026-08-22 (second pass) — 100% agreement was the next problem.** Fixing the
   reference took the gate to 100% within tolerance, which sounds like success and
   is closer to a dead instrument. Two linked reasons:

   * **No human ceiling.** Judge-vs-human agreement means nothing on its own. If
     two careful annotators agree α=0.85 with *each other*, a judge at 0.84 is at
     human level; at 0.99 it has fitted one annotator's idiosyncrasies. We could
     not tell which world we were in.
   * **The cases were binary.** Measured distribution: **16 fives, 15 at or below
     2, exactly ONE midscale**. Good responses averaged 121 characters, bad ones
     38 — the bad ones are terse dismissals ("Sure, $50 is fine"). A judge, a
     human, or a length heuristic separates those trivially, so the gate sat
     pinned at its ceiling and could only ever move *down*.

   Those compound: a second annotator on binary cases would also agree ~100% and
   teach us nothing. So gold **v3** does both — a per-annotator `annotations`
   schema (`human_score` becomes the derived median) plus **20 deliberately
   ambiguous cases** targeting the missing 3-4 band: right limit with a subtly
   wrong verdict, correct verdict omitting the submitted-and-flagged consequence,
   multi-category requests, exactly-at-the-limit amounts, hedged phrasing, and
   verbose-vs-terse pairs of the *same* correct answer (which must score alike —
   a direct probe for the style-dependence found in `geap_tool_use`).

   `calibration.annotator_reliability` reports the human α by reusing
   `judge_panel.krippendorff_alpha_interval` **unchanged**, so the human α and the
   judge-panel α are the same statistic and directly comparable.
   `ceiling_verdict` then phrases judge agreement as at / below / above that
   ceiling — and treats *above* as suspicious rather than good.
   `python -m src.eval.annotate` collects the second pass: blind (no existing
   score is ever rendered), grounded in the real `POLICY_LIMITS` table, resumable,
   and non-destructive until an explicit `--merge`.

   **Open:** the 20 hard cases are unscored, so α is `nan` and the gate currently
   runs on the 32 contrast cases only. `DEFAULT_MIN_WITHIN_TOLERANCE = 0.7` must be
   re-derived from the measured ceiling once they are annotated — the headline
   number *will* drop when they land, by design. And a1/a2 are the same operator's
   two passes: independent of each other's labels, not of each other's framing.

### Rubric audit (2026-08-22)

All five judge rubrics reviewed against the system they grade. Two had defects;
**two were left alone deliberately** — an early attempt to "improve" the policy
rubric wholesale dropped calibration from 96.9% to 81.2% (r 0.941 → 0.694), so
every change below was gated on re-measuring.

| rubric | verdict |
| --- | --- |
| `policy_compliance` (`batch_eval.py`) | **fixed.** Its fallback ("if the query is not about expenses, rate on whether the agent correctly *routes* the request") was both vague and stale — the coordinator has no delegation — so the judge applied policy criteria to non-expense turns. A hotel *search* returning compliant results scored **0.2** for "no awareness of the policy". Now scopes policy judgement to expense questions/checks/submissions, states that a reply asking for a missing amount is correct, and that refusing to submit an over-limit expense is a defect. Search **0.2 → 1.0**, clarify **0.2 → 1.0**, hallucinated-limit control held at 0.2, calibration **100%, r 0.980**. |
| `geap_tool_use` (`batch_eval.py`) | **fixed.** It asked "did the agent call the right tool?" but `run_inference` supplies text only, **no trajectory** — so it graded narration. The identical correct answer scored **0.2** without and **1.0** with the words "I checked the policy". Reframed to grade the *evidence* a correct tool leaves in the response (real ids, fares, the actual limit, a confirmation), with an explicit "no tool needed" path for greetings and clarifying questions, and the unobservable call-ordering criterion moved out. Now: good 1.0 / incomplete 0.6 / wrong-route 0.4 / fabricated 0.2 — a real gradient instead of a style signal. Likely explains much of why `tool_use_accuracy` was the weakest monitored metric. |
| `tool_faithfulness` | **unchanged, sound.** The only rubric given the real trajectory, so it asks a question its inputs can answer. Already spells out what does *not* count as a claim. Live: 4.846 with 2/39 flagged. |
| `helpfulness` (online-only) | **unchanged.** Probed with a clarifying question and a greeting — both 1.0, no blind spot found. Its "judge only helpfulness (not tone or policy)" scope line is doing real work. |
| recall (`verify_cross_session_recall`) | **unchanged**, written the same week with explicit negative cases (a denial is not recall). |

The pattern across both defects: **a rubric asked for something its inputs could
not support**, and the judge answered anyway. Faithfulness avoids it by having
the trajectory; the other four have to stay inside what the response text shows.

### Statistical power (2026-08-22)

P1.5 shipped confidence intervals and a sample floor. Auditing where they were
actually *used* turned up three problems, and the third is the interesting one.

**The intervals were decorative.** `verify_monitors` computed `low_confidence`
and only rendered it as a `⚠`; it never touched `status`. A metric scored over 3
samples alerted exactly like one over 300.

**Three surfaces had no interval at all** — including `calibration.py`, which is
a PASS/FAIL *gate*, plus `publish_router_efficiency` and `tool_faithfulness`,
which both feed alerts.

**`MIN_SAMPLES = 8` is threshold-blind, and that hid a dead metric.**
`routing_accuracy_pct` was computed from **12** cases against an **80%** alert. At
n=12 the Wilson interval spans 80% for *every possible outcome* — 12/12, 11/12,
10/12, 9/12 — so a perfect score was statistically indistinguishable from a
failing one, and one case flipping moved the metric 8.3 points. `is_low_confidence(12)`
returned `False`, so nothing flagged it. **A blanket count cannot catch this; only
a threshold-relative check can.**

`stats.resolves_threshold` / `min_n_for_threshold` / `power_report` ask the
threshold-relative question, and `mean_power_report` does it for a gauge's mean.
Behaviour is **suppress-and-escalate**: a metric whose interval spans its
threshold does not alert, and *must* appear in `verify_monitors`'
`insufficient_power` block with the sample size that would settle it. Suppression
without escalation is how a monitoring gap is created — the failure shape this
repo keeps hitting — so the escalation half is not optional.

The calibration gate is now three-valued: `PASS` / `FAIL` / `INCONCLUSIVE`, the
last exiting 0 (an underpowered gate must not block CI on noise) but naming which
side the point estimate leans, so a real degradation cannot hide behind a neutral
word.

`ROUTER_EVAL_CASES` grew **12 → 40**, balanced across the three complexity bands,
which makes the 80% alert resolvable. That moves a published series — expect a
step change — and it exposed a gap in the dataset manifest: it covered evalset
JSON but not the **Python case lists that feed a monitored series**. Those are now
checksummed too, so growing them is a versioned act.

**A correction to numbers reported earlier in this work.** The rubric iteration
quoted 68.8% → 96.9% → 100% as successive improvements. At n=32 the last two are
not distinguishable (CIs [84.3%, 99.4%] and [89.3%, 100%]). The headline finding
survives — 68.8% and 96.9% have *disjoint* intervals, so the gold-set correction
was real and large — but "improved on every metric" was reading noise. The
original FAIL was also unsound: 68.8%'s interval contains the 70% floor.

**One design attempt failed and is worth recording.** The first power check
modelled health as a *share of good points* against a 0.9 floor. Resolving that
needs ~35 points, so a perfectly healthy 24-point series read "underpowered" and
**every** alert would have been suppressed — the escalation list permanently full
and therefore ignored. The claim under test is about the metric's *value*, not a
share, so the check moved to a bootstrap CI on the mean.

### P2 — larger / infra
7. **Expand + version datasets.** Grow adversarial + multi-turn + a long-context
   stress set; add `dataset_version` + checksum + a committed generation/curation
   script; split a frozen *regression* set from a *development* set (G2).
8. **Statistical alert baselining.** ✅ **implemented** (`src/eval/baseline.py`,
   `docs/notes/online-infra-empty-and-baseline-alerts.md`) — a rolling-baseline
   z-score anomaly block in `verify_monitors` alongside the static floor, and
   infra-empty responses partitioned out of the quality mean into their own
   `agent_online_eval/infra_empty_rate` ceiling, so a helpfulness alert means
   quality and not an empty stream (G4, G5).
9. **Wire multi-turn + a smoke online-monitor into advisory CI** so multi-turn and
   empty-stream regressions are caught per-PR (G5). ⏳ *open.*
   The second half — **replace the substring recall check with a judge** — is
   ✅ **done**: `verify_cross_session_recall.evaluate_recall` now grounds a
   deterministic judge on the facts Memory Bank actually holds. The substring check
   it replaced could not tell recall from its opposite ("I have no **window** seat
   preference on file" contained `window`, so it *passed*) on a check marked
   critical in `demo_readiness --deep`. Fails closed on an empty stream, a judge
   error, or an unparseable verdict; signals are now diagnostics, never the verdict.
   Still open within this item: running the judge over *multiple* recall probes
   rather than one.
10. **Tool-call faithfulness (hallucinated-action detection).** ✅ **implemented**
    (`src/eval/tool_faithfulness.py`, `tests/test_tool_faithfulness.py`) — a
    grounded judge compares completion claims against the real `stream_query`
    trajectory and flags fabricated actions; published on both offline
    (`agent_eval/tool_faithfulness`) and online (`agent_online_eval/*`) surfaces at
    the shared 3.0 floor (G7). The Branch-A/B trajectory-visibility fork it depended
    on was **resolved live 2026-08-18 → Branch A**: the coordinator surfaces nested
    domain MCP calls client-side, so faithfulness is action-level as designed.

## Honest caveats
- Rubric scoring needs a **deployed engine** (no local inference path — memory
  `eval-requires-deployed-engine`), so any CI-side robustness gain still costs a
  deploy or a shared engine.
- A judge panel multiplies judge cost per case (N judges × M samples); the
  `--sample-rate`/`--limit` levers already exist to bound it.
- Bigger datasets raise eval wall-clock and Vertex eval cost — the demo-scale
  choice was deliberate; robustness here is a cost/confidence trade, stated
  explicitly.

Related: [online-quality-monitor.md](./online-quality-monitor.md),
[offline-eval-monitoring-bridge.md](./offline-eval-monitoring-bridge.md),
[coordinator-tool-use-quality.md](./coordinator-tool-use-quality.md),
[coordinator-model-bakeoff.md](./coordinator-model-bakeoff.md).
