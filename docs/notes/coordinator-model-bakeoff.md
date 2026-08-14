# Coordinator model bake-off: Gemini vs Claude

A head-to-head that compares two Coordinator Agent deployments differing **only**
by backbone — `gemini-3.6-flash` (baseline) vs `claude-sonnet-5` (candidate) —
across three axes: offline rubrics, a native pairwise side-by-side (SxS)
win-rate, and per-model-labeled production traffic. `run_bakeoff` owns the
deploy/teardown lifecycle directly; it still borrows the `model_backend` factor
from the [DOE framework](./doe-framework.md) to define the two backbones in one
place, but it does **not** run the DOE PipelineJob fan-out (see below).

## Why persistent deploys, not the DOE pipeline

An earlier design ran the bake-off *as* a single-factor `full` DOE — two design
points → two `PipelineJob`s. That path is broken for this use case: the per-point
KFP pipeline deploys an **ephemeral** engine in `resolve_agent` and deletes it in
its `cleanup` exit-handler, so the engines are gone before pairwise/traffic can
reach them — and the DOE launcher never records an `engine_id` in the manifest.
Pairwise's `load_engines_from_manifest` then raises "manifest must have a gemini
and a claude point, each with an engine_id".

So the bake-off now deploys **two persistent engines itself**:

- Each backbone deploys in **its own interpreter** via
  `src.doe.deploy_coordinator` (subprocess-per-point, the same pattern as
  `src.doe.launch`), because `COORDINATOR_MODEL` is read once at import time in
  `src.config` and baked into the engine's `env_vars` — two backbones need two
  processes. The child prints its resource on a `BAKEOFF_ENGINE:` marker line the
  parent recovers from stdout.
- `deploy_coordinator` uses `deploy_agent` (which does **not** write `.env`), so
  the two coordinators coexist without clobbering the shared
  `COORDINATOR_AGENT_ID`/`AGENT_ENGINE_ID` keys. Both `engine_id`s are recorded in
  the run **manifest** (`doe_runs/<exp>/manifest.json`), which `pairwise_eval
  --from-manifest` reads (gemini=baseline, claude=candidate).
- Both engines are **torn down in a guaranteed `finally`** (`agent_engines.delete(
  ..., force=True)`, each wrapped in try/except so one failure doesn't strand the
  other). `--keep-engines` opts out — e.g. to re-run pairwise by hand.

Level order still fixes the roles: gemini is coded `-1` (baseline), claude `+1`
(candidate), so a pairwise "candidate wins" reads as "Claude beats Gemini".

## The three evidence streams

1. **Offline rubrics (per engine).** `run_bakeoff` scores each *deployed* engine
   directly with `multi_agent_batch_eval` (Vertex Gen AI Evaluation Service, 6
   batch rubrics) — `_score_engine` calls `run_multi_agent_batch_eval(agents=
   ["coordinator_agent"], agent_id=<this engine>)`, and `_quality_from_batch` maps
   the versioned metric keys (`.../tool_use_quality_v1`) to canonical base names
   (via `harvest._metric_base` + `BATCH_METRICS`) so both engines share keys for
   the delta math.

   **Cost is measured, not assumed.** `collect_token_usage` reads real
   `usage_metadata` off each engine's `stream_query` (running-max
   `prompt_token_count` → input tokens; summed `candidates_token_count` across
   tool-call/thinking/answer events → output tokens) over the eval prompts, then
   `src/eval/cost_model.py` prices it: Gemini per-token; Claude via GSU burndown
   (1 GSU/input tok, 5 GSU/output tok at <200k ctx) → USD. If **no** usage
   surfaces, `_cost_from_usages` returns `None` so the report shows an honest
   `n/a` rather than a fake `$0`. This isolated `stream_query` pass lives in the
   bake-off (not the shared `_sdk_patches.py`) to avoid blast radius. Pricing
   constants are **directional — verify against live pricing before quoting** any
   stakeholder-facing number.

2. **Pairwise SxS win-rate** (`src/eval/pairwise_eval.py`). For each case both
   engines answer, then a `PairwiseMetric` autorater (`flip_enabled=True`,
   `sampling_count=4`) picks a winner → aggregate win rate. If the managed
   pairwise template 400s (the same SDK JSON-parser failure that forced
   `policy_judge` standalone), it falls back to a standalone `google.genai` judge
   emitting `Choice: A|B|TIE`. `--from-manifest` auto-picks gemini=baseline,
   claude=candidate.

3. **Per-model-labeled traffic (online).** `generate_traffic --label model=<id>`
   stamps every emitted `agent_traffic/*` series with the model, so the two
   deployments stay **separate Cloud Monitoring series** instead of collapsing
   into one mean. `verify_monitors --group-by model` reads them back as two
   buckets; the dashboard grows per-model breakdown tiles (grouped by
   `metric.label.model`) alongside the existing aggregate widgets.

## The report

`src/doe/bakeoff_report.py` is pure assembly (no network): it fuses the four
streams — offline rubric means + delta, pairwise win-rate, online p50/p95
latency + error rate, and per-request $ — into `bakeoff_report.md` with a
one-line verdict (e.g. "claude-sonnet-5 wins offline quality by 0.150 avg
rubric; wins SxS at 60%; costs 5.0x more; adds 1000 ms p95"). Any per-model
input may be empty (offline-only run) → missing cells render `n/a`.

## Experiments record (durable side-by-side)

Beyond the markdown report, `run_bakeoff` records **one Vertex AI Experiments run
per backbone** so the comparison survives as a queryable, console-viewable
artifact — open **Vertex AI → Experiments → `coordinator-bakeoff` → Compare
runs** to see gemini vs claude side-by-side. Each run logs params
(`backbone=<model_id>`, `role=baseline|candidate`) and scalar metrics (rubric
means, the backbone's own `pairwise_win_rate`, `p50/p95_latency`, `error_rate`,
`cost_per_request`) via `src/observability/experiments.py:log_run` — a thin,
best-effort wrapper over `google.cloud.aiplatform` (`init → start_run →
log_params → log_metrics`). Summary metrics need **no** Managed TensorBoard.

- **Separation is strict.** The coordinator logs to `coordinator-bakeoff`; the
  router's efficiency series belongs in a **separate** `router-efficiency`
  experiment — the two are never mixed in one run. `log_run` is generic; the
  caller picks the experiment name.
- **Dormant by default, best-effort always.** `log_run` is a clean no-op (no SDK
  call, no billable resource) when no experiment name is passed; the bake-off's
  CLI defaults `--experiment-name coordinator-bakeoff` so live `--execute` runs
  record, and `--experiment-name ''` disables it. A logging failure only prints a
  warning — a completed report and engine teardown are never undone by the
  side-record.

## One command

`src/doe/run_bakeoff.py` chains all phases, **dry-run by default** (prints the
plan, deploys/spends nothing — mirrors `run_doe`). Live order: preflight both
backbones → deploy 2 engines → offline rubrics + cost per engine → pairwise →
labeled traffic → grouped verify + report → teardown.

```bash
# Plan only (default): deploy/spend nothing
uv run --group doe python -m src.doe.run_bakeoff

# Full live bake-off: two deploys → offline + cost + pairwise + traffic → report → teardown
uv run --group doe python -m src.doe.run_bakeoff --execute

# Keep the two engines alive afterwards (e.g. to re-run pairwise by hand)
uv run --group doe python -m src.doe.run_bakeoff --execute --keep-engines
```

Every phase entrypoint is injectable (`preflight_fn`/`deploy_fn`/`score_fn`/
`usage_fn`/`pairwise_fn`/`verify_fn`/`traffic_runner`/`teardown_fn`/`log_run_fn`),
so the
orchestration wiring — including guaranteed teardown on mid-run failure — is
unit-tested without touching GCP. (`--wait` is a no-op kept for CLI compatibility;
the persistent-deploy path is synchronous.)

## Honest caveats

- **Dataset is ~50 curated cases, not ≥1000.** Enough for a demo-honest
  comparison and to surface directional deltas; a true model-upgrade benchmark
  wants a far larger set (Google guidance ≥1000). Cases now include multi-step /
  multi-intent and adversarial / prompt-injection categories, all with
  references.
- **Pairwise judge is Gemini-only** — a single autorater family; flip debiasing
  + multi-sampling mitigate but don't eliminate judge bias.
- **Pricing constants need live verification** (see cost model note above).
- **No native endpoint traffic-split.** A bare Agent Engine has no managed
  traffic-splitting, so we drive the per-model split ourselves (one labeled
  traffic run per engine) rather than relying on a platform A/B feature.
- **Online eval content is platform-blocked** — the offline bridge is the
  canonical quality source; see [[online-eval-content-capture-blocked]] and the
  [offline-eval → monitoring bridge](./offline-eval-monitoring-bridge.md) note.
- **Newly-deployed coordinator engines don't stream (platform-side regression,
  2026-08-13).** Every coordinator engine **built after ~09:44 UTC 2026-08-13**
  returns 0 events / empty streams — the managed Agent Engine worker is hard-killed
  (SIGKILL, no traceback) at the first LLM call. This is **backbone-independent**:
  fresh `gemini-3.6-flash`, `gemini-3.5-flash`, and `claude-sonnet-5` engines all
  fail identically, while the pre-rollout `gemini-3.6-flash` engine
  `3639024497392091136` (built 09:44) still streams cleanly. Dependency versions
  and agent code are proven identical between the working and failing engines (PyPI
  timestamps show no package changed in the window), so the only differentiator is
  build time — a Google-side runtime/base-image rollout, **not** the model and
  **not** `enable_tracing` (removing the flag did not restore streaming). The
  models themselves serve fine standalone via LiteLLM (verified with
  `src.eval.preflight`). Impact on the bake-off: it cannot deploy fresh per-model
  engines to produce offline/pairwise/traffic signal until the platform issue
  clears; re-verify with a `stream_query` probe (expect events>0) before running
  `run_bakeoff --execute` again. See [[coordinator-outage-is-runtime-not-model]].
