# Infra-empty separation + rolling-baseline alerts (P2.8)

**Problem this closes.** Two ways the online-quality surface (`agent_online_eval/*`)
lied about health:

1. **Empty streams scored as low quality.** A cold-start / high-complexity timeout
   returns an *empty* (0-char) or error-shaped (`{"error": ...}`) response at
   **HTTP 200**. The old monitor fed those straight to the helpfulness rubric, so
   an infrastructure failure was averaged into the *quality* mean and paged as a
   model regression. A real run (2026-08-15) had 12/31 empties (39%) drag online
   helpfulness to 2.87 vs offline 4.03 — see memory
   `online-helpfulness-dips-are-empty-streams`.
2. **Static floors miss drift.** `quality_alerts.py` alerts only on an absolute
   line (e.g. helpfulness `< 3.0`). A slow slide from 4.6 → 3.4 is a real
   regression that never trips the floor, and a single sub-floor blip can't be
   told apart from normal variance.

## Part A — separate infra-empty from quality-low

`src/eval/online_monitor.py`:

- `is_infra_empty(response)` — `True` for an empty/whitespace or `{"error"`-shaped
  response (mirrors `policy_judge._is_error_response`).
- `partition_interactions(pairs)` → `(real, empty)`. **Only `real` pairs reach the
  judges**, so an empty-at-200 can never touch the quality mean.
- `score_and_publish` now returns `n_infra_empty` + `infra_empty_rate` and an
  `infra_published` block, and publishes the rate as its own signal.
- `publish_infra_empty_rate(rate)` writes `agent_online_eval/infra_empty_rate`
  **verbatim (0-1, no 1-5 scaling)** via
  `src/observability/metrics.py:write_online_infra_metrics`.

`src/eval/quality_alerts.py`:

- `ONLINE_INFRA_METRICS = [("infra_empty_rate", 0.2, "GT")]` — a **ceiling** alert
  (`GT`): page when more than a fifth of sampled live traffic comes back empty.
  Same `agent_online_eval/*` family as the quality rubrics, different axis.
  `setup_all_alerts` creates it alongside the LT quality floors.

`src/eval/verify_monitors.py` reads `infra_empty_rate` back on the `online_quality`
surface (GT direction), so it shows up in `--format json` and the text report.

## Part B — rolling-baseline z-score anomaly detection

`src/eval/baseline.py` (pure stdlib, no GCP):

- `mean` / `stddev` (sample, ddof=1) / `zscore` (None on a flat baseline).
- `detect_regression(history, current, *, direction, z_threshold=2.0,
  min_baseline=5)` — one-sided per the metric's alert direction: `LT` metrics flag
  a **drop** (`z <= -2`), `GT` metrics flag a **spike** (`z >= +2`). Returns a
  `status` of `insufficient_history` (< 5 priors), `no_variance` (flat baseline),
  or `ok` with `is_anomaly`.

`verify_monitors._summarize` treats the chronologically-latest point as `current`
and everything before it as the baseline history, then attaches a `baseline` block
and `current_score` to each metric summary. It is **additive** — the static-floor
`out_of_bounds` count is untouched; the baseline just catches drift/step-changes
the floor misses.

## Deliberately NOT touched

- **No live alert-policy mutation in code paths under test.** The new alert is
  declared in `ONLINE_INFRA_METRICS` and only created when someone runs
  `setup_all_alerts` — the pure classifier/partition/baseline logic that ships in
  the monitor never calls Cloud Monitoring.
- The native Online Evaluators / content-capture story is unchanged
  ([online-eval-content-capture.md](./online-eval-content-capture.md)).

## Caveats

- `infra_empty_rate` is computed over the *sampled* interactions, so at low sample
  rates it's a noisy estimate — read it with the sample count, not alone.
- The baseline uses the trailing verify window (default 24h) as history; it needs
  `min_baseline=5` prior points before it renders any verdict, so a freshly-seeded
  metric reads `insufficient_history` (correctly, not a false all-clear).
- z-score assumes roughly-stationary recent history; a legitimate step-change
  (e.g. a model upgrade) will flag once until it becomes the new baseline.

## Tests

- `tests/test_baseline.py` — mean/stddev/zscore + direction-aware `detect_regression`.
- `tests/test_online_monitor.py` — `is_infra_empty`, `partition_interactions`,
  infra-empty excluded from the quality mean, `infra_empty_rate` published verbatim.
- `tests/test_online_eval.py` — `verify_monitors` surfaces the baseline anomaly
  block and the `infra_empty_rate` GT ceiling.
- `tests/test_quality_alerts.py` — `setup_all_alerts` splits online quality (LT)
  from online infra (GT).
