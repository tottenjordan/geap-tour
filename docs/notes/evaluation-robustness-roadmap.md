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
4. **Judge panel / self-consistency + report agreement.** Either (a) a 3-model
   panel (e.g. `gemini-2.5-flash` + a Gemini-3 tier + a Claude tier) with
   median/majority, or (b) N-sample self-consistency of one judge with the
   **median + variance** reported. Compute inter-rater agreement
   (Krippendorff/Cohen). Reuses the `pairwise_eval.py:48` `sampling_count` pattern.
5. **Confidence intervals + sample-size floors.** Bootstrap CI on each aggregate;
   refuse pass/fail (or mark `low_confidence`) when `n < floor`; add a binomial
   significance test to the pairwise win-rate. Surfaces in `verify_monitors`.
6. **Human-label calibration set.** ~30 gold cases with human labels; track
   judge-vs-human accuracy over time so autorater drift/bias is measurable, not
   assumed (G1).

### P2 — larger / infra
7. **Expand + version datasets.** Grow adversarial + multi-turn + a long-context
   stress set; add `dataset_version` + checksum + a committed generation/curation
   script; split a frozen *regression* set from a *development* set (G2).
8. **Statistical alert baselining.** Replace static floors with rolling-baseline +
   z-score / regression-vs-last-good-release; and **label infra-empty separately**
   from quality-low in the online monitor so a helpfulness alert means quality
   (G4, G5).
9. **Wire multi-turn + a smoke online-monitor into advisory CI** so multi-turn and
   empty-stream regressions are caught per-PR (G5), and **replace the substring
   recall check with a judge** over multiple recall probes.

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
