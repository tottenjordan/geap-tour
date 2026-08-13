# Coordinator model bake-off: Gemini vs Claude

A head-to-head that compares two Coordinator Agent deployments differing **only**
by backbone — `gemini-3.6-flash` (baseline) vs `claude-sonnet-5` (candidate) —
across three axes: offline rubrics, a native pairwise side-by-side (SxS)
win-rate, and per-model-labeled production traffic. It reuses the existing
[DOE framework](./doe-framework.md) rather than a bespoke harness.

## Why the DOE framework carries it

The bake-off is a **single-factor `full` DOE**: the new `model_backend` factor
(coordinator-only; moves just `COORDINATOR_MODEL`, leaving the travel/expense
sub-agents fixed) has two levels, so `build_design([model_backend], kind="full")`
→ **exactly 2 design points, no baseline replicate** → two fresh Agent Engine
deploys, one per model. The DOE **manifest** (`doe_runs/<exp>/manifest.json`)
records each point's own `engine_id` + `PipelineJob`, which is what lets two
coordinators coexist — deploying twice through `deploy_agents.py` alone would
clobber the shared `COORDINATOR_AGENT_ID`/`AGENT_ENGINE_ID` keys in `.env`.

Level order fixes the roles: gemini is coded `-1` (baseline), claude `+1`
(candidate), so every DOE main effect is `claude_mean − gemini_mean` and a
pairwise "candidate wins" reads as "Claude beats Gemini".

## The three evidence streams

1. **Offline rubrics (per engine).** The per-point Vertex pipeline scores each
   *deployed* engine via the Gen AI Evaluation Service (6 batch rubrics +
   simulated + complexity) and harvests `full_results.json` → `results.csv`.
   Two correctness fixes make the per-engine scores honest:
   - `publish_offline_eval._inject_policy_compliance` now takes an explicit
     `agent_id` so the policy judge scores the *run's* engine, not the `.env`
     default.
   - a fair per-request **cost model** (`src/eval/cost_model.py`): Gemini is
     priced per-token; Claude via GSU burndown (1 GSU/input tok, 5 GSU/output tok
     at <200k ctx) → USD. Pricing constants are **directional — verify against
     live pricing before quoting** any stakeholder-facing number.

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

## One command

`src/doe/run_bakeoff.py` chains all five phases, **dry-run by default** (prints
the plan, deploys/spends nothing — mirrors `run_doe`):

```bash
# Plan only (default): deploy/submit/spend nothing
uv run --group doe python -m src.doe.run_bakeoff

# Full live bake-off: two deploys → offline + pairwise + labeled traffic → report
uv run --group doe python -m src.doe.run_bakeoff --execute --wait
```

Each phase entrypoint is injectable, so the orchestration wiring is unit-tested
without touching GCP.

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
