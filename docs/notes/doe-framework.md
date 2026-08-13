# DOE framework for scaling experimentation

The DOE (Design of Experiments) framework turns the single-config Vertex eval
pipeline into an experiment engine: it varies agent-config **factors**, fans out
**one `PipelineJob` per design point**, harvests each run's `full_results.json`
from GCS, and reports **main effects** + the **cost-quality frontier**. Code
lives in `src/doe/`; the eval pipeline is *extended*, not rewritten.

## Layout

- `src/doe/factors.py` — declarative `Factor` registry (name, channel, 2 levels).
  Four seed factors: `router_boundaries` (runner_env), `model_tier` (engine_env),
  `prompt_variant` (engine_env), `eval_fidelity` (param). Plus `model_backend`
  (engine_env, coordinator-only) for the Gemini-vs-Claude bake-off — see the
  [coordinator model bake-off](./coordinator-model-bakeoff.md) note.
- `src/doe/design.py` — `build_design(factors, kind)`: `screening` →
  resolution-IV `2^(4-1)` = 8 runs + 1 baseline anchor = **9**; `full` →
  `ff2n` = 16. Coded `-1`→low label, `+1`→high label.
- `src/doe/launch.py` — one `src.pipelines.submit` **subprocess per design
  point** (fresh interpreter bakes that point's env at compile time). Parses the
  `Submitted PipelineJob: <resource>` last stdout line into a manifest.
- `src/doe/harvest.py` — polls each job to terminal, pulls `full_results.json`,
  builds a tidy one-row-per-point DataFrame → CSV (local + GCS).
- `src/doe/analyze.py` — main effects (nanmean high − low), factor ranking,
  Pareto cost-quality frontier, recommended config → `report.md`.
- `src/doe/run_doe.py` — orchestrator CLI (design → launch → harvest → analyze).

## Factor channels (why some points need a fresh deploy)

The channel decides *how* a factor reaches the run — and whether it forces a
fresh Agent Engine deploy:

- **`engine_env`** (`model_tier`, `prompt_variant`) — reconfigures the deployed
  engine. The engine reads `src/config.py` at **import time inside its
  container**; there is no request-time env re-read. So these factors ⇒ a
  **fresh engine per design point**. `requires_fresh_deploy(factors)` is True if
  any active factor is `engine_env`.
- **`runner_env`** (`router_boundaries`) — affects the in-runner `complexity`
  eval only (via `complexity.py`); no engine deploy needed.
- **`param`** (`eval_fidelity`) — already pipeline parameters
  (`scenario_count`, `max_turns`); passed via `parameter_values`, no deploy.

## Why subprocess-per-point (not reload-in-process)

`set_env_variable` in KFP takes compile-time strings, not pipeline params, and
`src/config.py` caches env at import. A fresh `submit.py` subprocess with that
point's env baked into a **unique `--spec-path`** is the deliberate fix — it
sidesteps import-time caching *and* the shared-spec write race. Do **not** try
to `reload()` config in one process for live runs.

## Config-injection layer (Phase 1)

To make factors overridable, `src/config.py` gained env-read constants
(`COORDINATOR/TRAVEL/EXPENSE/ROUTER_MODEL`, `COMPLEXITY_LOW/HIGH`,
`MEDIUM_SPLIT`, `HIGH_SPLIT`, `PROMPT_VARIANT`), and `deploy_agents.py`
`_build_config` now **bakes all of them** into the engine `env_vars` (this also
closed a pre-existing gap where `AGENT_MODEL` was never baked). Prompt baselines
were recovered from `366013c^`; `PROMPT_VARIANT` toggles the travel/expense
sub-agents only (the coordinator was never GEPA'd).

## Response-variable JSON paths (in `full_results.json`)

- batch: `results["batch"]["agents"][AGENT]["metrics"][KEY]["score"]` where
  `KEY` = `agent_engine_0/<metric>_v<N>`. The `agent_engine_0/` prefix and
  `_vN` suffix are API-assigned — **match on the version-stripped base name**
  (`final_response_match` is `_v2`, the rest `_v1`).
- complexity: `results["complexity"]["accuracy"]["accuracy"]` and
  `["cost_efficiency"][{savings_pct,routed_cost_usd,all_opus_cost_usd}]`.
- simulated: `results["simulated"][AGENT]["passed"]`.

Anything missing/malformed → NaN, so one bad run never sinks the harvest.

## Running it

```bash
# Dry run — prints the 9-point design + cost estimate, submits nothing (default)
uv run --group doe python -m src.doe.run_doe --kind screening --experiment-id smoke

# Cheap smoke (2 real jobs) before the full screening — opt-in via --execute
uv run --group doe python -m src.doe.run_doe --kind screening --execute --max-runs 2 --wait

# Full screening (9 jobs)
uv run --group doe python -m src.doe.run_doe --kind screening --execute --wait

# Single-factor bake-off (2 deploys, one per model) — see the bake-off note
uv run --group doe python -m src.doe.run_bakeoff              # dry-run plan
uv run --group doe python -m src.doe.run_bakeoff --execute --wait
```

**Cost caveat:** `model_tier` + `prompt_variant` are `engine_env`, so 8 of 9
screening runs deploy a fresh Agent Engine + traffic + simulated turns (heaviest
path). `run_doe.py` defaults to dry-run; real fan-out is opt-in (`--execute`).
Validate with the 2-run smoke first.

## Deps

DOE deps (`pyDOE3`, `pandas`, `numpy`) live in the **`doe` dependency group**,
kept out of the runtime image. CI (`.github/workflows/tests.yaml`) installs and
runs with `--group doe --group pipelines`.
