# DOE Framework for Scaling GEAP Experimentation & Evaluation — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: use superpowers:executing-plans to implement this plan task-by-task.
> On execution, copy this file to `docs/plans/2026-08-06-doe-framework.md`.

**Goal:** A Design-of-Experiments (DOE) framework that varies agent-configuration factors, fans out one Vertex `PipelineJob` per design point (reusing the just-validated eval pipeline as the per-run engine), harvests each run's `full_results.json`, and reports main effects / cost-quality trade-offs.

**Architecture:** A declarative factor registry → a design generator (fractional-factorial screening) → a launcher that submits N non-blocking pipeline jobs (each `submit.py` subprocess bakes that point's factor env at compile time and deploys a config-overridden agent variant) → a harvester that pulls tidy results from GCS → an analyzer. New code lives in `src/doe/`; the existing pipeline is extended, not rewritten.

**Tech Stack:** `pyDOE3` (design matrices), `pandas`/`numpy` (tidy results + effects), existing `kfp` + `google-cloud-aiplatform` fan-out, GCS for artifacts. DOE deps go in a `doe` dependency group (kept out of the runtime image).

---

## Context

We just validated the Vertex Managed Pipeline end-to-end (reuse-engine + `--skip-traffic` → `PIPELINE_STATE_SUCCEEDED`, report in GCS, no leaked engines). The first real run also produced genuine signal: every agent clears safety/hallucination/quality but falls **below the 0.6 bar on `tool_use_quality` and `final_response_match`** (e.g. coordinator 0.42/0.42), while complexity routing hits 100% accuracy / 63.2% cost savings. Those weak metrics are exactly what systematic experimentation should target.

The user wants to "scale experimentation and evaluation." Today each pipeline run tests **one** fixed configuration. A DOE framework turns the parameterized pipeline into an experiment engine: define factors × levels, run a designed subset of configurations, and attribute metric changes to factors instead of guessing. The four chosen factors: **router boundaries, model tier per sub-agent, prompt variant (baseline vs GEPA), and eval fidelity.** Variant strategy: **extend the pipeline** so `resolve-agent` deploys config-overridden variants automatically.

### Grounding facts (from exploration — do not re-derive)

- **Variants require deploys.** A deployed agent bakes model/prompt/router-boundaries at *import time* inside the engine container (`coordinator_agent.py:58-69`); there is **no** request-time env re-read. So model/prompt factors ⇒ **fresh engine per design point**.
- **The only channel to the engine is `deploy_agent`'s `env_vars` dict** (`deploy_agents.py:_build_config`, ~`:101-132`). It currently **omits `AGENT_MODEL`, router boundaries, and any prompt toggle** — these must be added.
- **`set_env_variable` cannot take a pipeline parameter** (compile-time strings only) — but `submit.py` recompiles every run. Each design point runs as its **own `submit.py` subprocess** with its own env → its own baked spec → its own non-blocking `PipelineJob`. Fresh interpreter per subprocess sidesteps import-time-env caching and the shared `eval_pipeline.yaml` write race (give each a unique `--spec-path`).
- **Bookkeeping *can* be pipeline parameters.** `experiment_id` / `design_point` go through `parameter_values` → threaded to the `report` component → deterministic GCS path for harvest.
- **Factor channels differ:** router boundaries affect the in-runner `complexity_eval` (via `complexity.py`) — **no engine deploy needed**; model tier & prompt variant reconfigure the **deployed engine** — deploy needed; eval fidelity is already **pipeline parameters** (`scenario_count`, `max_turns`, `skip_traffic`, `traffic_count`). The factor registry encodes this per factor.
- **Prompt baseline is recoverable:** GEPA prompts landed in commit `366013c`; baselines live at `366013c^:src/agents/{travel,expense}_agent.py`. The **coordinator was never GEPA'd** — `PROMPT_VARIANT` toggles the travel/expense sub-agents only.
- **Response-variable JSON paths** in `full_results.json` (exact): batch `results["batch"]["agents"][AGENT]["metrics"]["agent_engine_0/<metric>_vN"]["score"]` (0–1); complexity `results["complexity"]["accuracy"]["accuracy"]`, `["cost_efficiency"]["savings_pct"|"routed_cost_usd"|"all_opus_cost_usd"]`; simulated `results["simulated"][AGENT]["passed"]`. `agent_engine_0/` prefix is API-assigned — match on the `*_vN` suffix.

---

## Phase 1 — Config-injection layer (make the four factors overridable + bake into deploys)

### Task 1: Per-agent model + router-boundary + prompt-variant config constants
**Files:** Modify `src/config.py`
- Add, after the existing model constants: `COORDINATOR_MODEL`, `TRAVEL_MODEL`, `EXPENSE_MODEL` each `os.environ.get("<NAME>", AGENT_MODEL)`, and `ROUTER_MODEL = os.environ.get("ROUTER_MODEL", LITE_MODEL)`.
- Add boundary constants read from env (defaults = today's literals): `COMPLEXITY_LOW = float(os.environ.get("COMPLEXITY_LOW", "0.30"))`, `COMPLEXITY_HIGH = float(os.environ.get("COMPLEXITY_HIGH", "0.60"))`, `MEDIUM_SPLIT = float(os.environ.get("MEDIUM_SPLIT", "0.45"))`, `HIGH_SPLIT = float(os.environ.get("HIGH_SPLIT", "0.80"))`. (Reuse/rename the orphaned `COMPLEXITY_THRESHOLD_HIGH` rather than leave two.)
- Add `PROMPT_VARIANT = os.environ.get("PROMPT_VARIANT", "gepa")`.
- **Test** `tests/test_config_overrides.py`: with `monkeypatch.setenv` + `importlib.reload(src.config)`, assert each new constant reflects the env override and falls back correctly.

### Task 2: Router boundaries read from config
**Files:** Modify `src/router/complexity.py`
- Replace literals `THRESHOLDS = [0.30, 0.60]`, `MEDIUM_SPLIT = 0.45`, `HIGH_SPLIT = 0.80` (~`:52-57`) with imports from `src.config`. Fix `score_to_model_tier` (~`:72,76`) to use `THRESHOLDS[0]/[1]` instead of bare `0.30/0.60`.
- **Test** extend `tests/test_router.py` (or new `test_complexity_boundaries.py`): overriding `COMPLEXITY_LOW/HIGH`, `MEDIUM_SPLIT`, `HIGH_SPLIT` via env+reload shifts `score_to_model_tier` cut-points.

### Task 3: Agents consume per-agent model constants
**Files:** Modify `src/agents/coordinator_agent.py`, `travel_agent.py`, `expense_agent.py`, `src/router/agents.py`
- Swap `resolve_model(AGENT_MODEL)` → `resolve_model(COORDINATOR_MODEL)` / `TRAVEL_MODEL` / `EXPENSE_MODEL` respectively; in `router/agents.py` the `router_agent` uses `resolve_model(ROUTER_MODEL)` (tier sub-agents unchanged). Existing tests (`test_coordinator.py`, `test_router.py`) must stay green (defaults preserve behavior).

### Task 4: Prompt-variant toggle with recovered baseline
**Files:** Modify `src/agents/travel_agent.py`, `expense_agent.py`
- Recover baselines: `git show 366013c^:src/agents/travel_agent.py` / `expense_agent.py`, lift each `INSTRUCTION` body.
- In each module define `INSTRUCTION_GEPA = <current>` and `INSTRUCTION_BASELINE = <recovered>`, then `INSTRUCTION = INSTRUCTION_BASELINE if PROMPT_VARIANT == "baseline" else INSTRUCTION_GEPA`. Coordinator unchanged (no GEPA variant).
- **Test** `tests/test_prompt_variant.py`: `PROMPT_VARIANT=baseline` vs `gepa` (env+reload) selects the matching instruction string; unknown value → gepa.

### Task 5: Bake new env into deployed engines
**Files:** Modify `src/deploy/deploy_agents.py` (`_build_config`)
- Add to `env_vars`: `AGENT_MODEL`, `COORDINATOR_MODEL`, `TRAVEL_MODEL`, `EXPENSE_MODEL`, `ROUTER_MODEL`, `COMPLEXITY_LOW`, `COMPLEXITY_HIGH`, `MEDIUM_SPLIT`, `HIGH_SPLIT` (as `str(...)`), and `PROMPT_VARIANT`. This closes the "AGENT_MODEL never baked" gap and makes every factor reach the engine.
- **Test** extend `tests/test_deploy_agents.py` (or new): `_build_config` env_vars includes the new keys with values sourced from config.

**Commit per task** (`feat:`/`refactor:`), run `uv run --group pipelines pytest -q` green after each.

---

## Phase 2 — Pipeline bookkeeping + deterministic result paths

### Task 6: Factor env + experiment params in the pipeline
**Files:** Modify `src/pipelines/eval_pipeline.py`
- Extend `_wire()` to also bake a `_FACTOR_ENV` dict read from `os.environ` **at compile time** for every factor env key from Phase 1 (model/boundary/prompt vars) — mirrors the existing `_RUNTIME_ENV` pattern, so a `submit.py` subprocess that sets those env vars gets them baked onto every task (including `resolve_agent`, which then deploys the variant).
- Add pipeline params `experiment_id: str = ""`, `design_point: str = ""`; pass them into `c.report(...)`.

### Task 7: `report` writes to a DOE-keyed GCS path
**Files:** Modify `src/pipelines/components.py` (`report`)
- Add `experiment_id: str = ""`, `design_point: str = ""` params. When set, upload to `gs://{bucket}/eval-results/doe/{experiment_id}/{design_point}/{report.md,full_results.json}` (else keep today's `eval-results/{run_id}/`). Embed `experiment_id`/`design_point` into the results dict written to `full_results.json`.
- **Test** extend `tests/test_pipeline.py`: pipeline still compiles with the new params (KFP compile check).

### Task 8: `submit.py` factor + experiment passthrough
**Files:** Modify `src/pipelines/submit.py`
- Add `--experiment-id`, `--design-point`, `--spec-path` (default `eval_pipeline.yaml`), and pass-through of eval-fidelity args (`--scenario-count`, `--max-turns`, `--traffic-count`) into `parameter_values`. Factor **env** vars are already in the process env (set by the launcher) and get baked at compile — no CLI needed for those.
- Compile to `--spec-path` (unique per design point) to avoid the shared-file race. Print the `PipelineJob` resource name as the last stdout line (machine-readable for the launcher).
- **Commit** `feat: thread DOE factors + experiment tags through pipeline + submit`.

---

## Phase 3 — DOE core (`src/doe/`)

### Task 9: DOE dependency group
**Files:** Modify `pyproject.toml`
- `uv add --group doe pyDOE3 pandas numpy`; `uv sync --group doe`. Add `--group doe` to `.github/workflows/tests.yaml` install+run so DOE unit tests gate PRs.

### Task 10: Declarative factor registry
**Files:** Create `src/doe/__init__.py`, `src/doe/factors.py`
- Each factor: `name`, `channel` (`"engine_env"` | `"runner_env"` | `"param"`), `env` mapping or param name, and `levels` (label → concrete value(s)). Seed the four factors:
  - `router_boundaries` (runner_env): `baseline`={COMPLEXITY_LOW:0.30,MEDIUM_SPLIT:0.45,COMPLEXITY_HIGH:0.60,HIGH_SPLIT:0.80} vs `aggressive_savings`=shift cut-points up (more traffic to cheap tiers).
  - `model_tier` (engine_env): `baseline`={COORDINATOR_MODEL,TRAVEL_MODEL,EXPENSE_MODEL = flash} vs `upgraded`={... = pro}.
  - `prompt_variant` (engine_env): `PROMPT_VARIANT` baseline vs gepa.
  - `eval_fidelity` (param): `quick`={scenario_count:3,max_turns:2} vs `thorough`={scenario_count:8,max_turns:4}.
- Helper `requires_fresh_deploy(active_factors)` → True if any active factor is `engine_env`.
- **Test** `tests/test_doe_factors.py`: registry integrity (levels present, channels valid).

### Task 11: Design generator
**Files:** Create `src/doe/design.py`
- `build_design(factors, kind="screening")` → list of design points (each = `{factor: level_label}` + a stable `design_point` id). Use `pyDOE3.fracfact` for a resolution-IV `2^(4-1)` (8-run) screening design + one center/baseline point; `kind="full"` → `pyDOE3.ff2n` (16 runs). Map coded ±1 → level labels.
- **Test** `tests/test_doe_design.py`: screening → 8 (+1) points, full → 16; every point assigns all factors; ids unique.

### Task 12: Launcher (fan-out)
**Files:** Create `src/doe/launch.py`
- For each design point: assemble env (engine_env+runner_env level values) + fidelity params; `subprocess.run([...python -m src.pipelines.submit ...], env={**os.environ, **factor_env})` with `--experiment-id`, `--design-point`, unique `--spec-path`, and `--agent-module coordinator_agent` (fresh deploy when `requires_fresh_deploy`). Capture the printed job resource name.
- Write a manifest (`experiment_id`, per-point factors + job resource + expected GCS path) to `gs://{bucket}/eval-results/doe/{experiment_id}/manifest.json` and locally. Log a cost line: N runs, fresh-deploy count.
- **Test** `tests/test_doe_launch.py`: monkeypatch subprocess; assert per-point env/args are correct and manifest shape is right (no real submit).

### Task 13: Harvester
**Files:** Create `src/doe/harvest.py`
- Poll each manifest job via `aiplatform.PipelineJob.get(...)` until terminal; download each point's `full_results.json` from its GCS path; parse response vars with the exact paths above (suffix-match metric keys, tolerate error stubs → NaN). Emit a tidy `pandas.DataFrame` (one row per design point: factor columns + response columns) → CSV in GCS + local.
- **Test** `tests/test_doe_harvest.py`: parse a committed `tests/fixtures/full_results.json` (captured from our validated run) → assert extracted response values; malformed input → NaN, no crash.

### Task 14: Analyzer
**Files:** Create `src/doe/analyze.py`
- From the tidy table: per response, main effect of each factor = mean(high level) − mean(low level); rank factors by |effect|; flag the cost-quality frontier (savings_pct vs tool_use_quality / final_response_match). Write `report.md` (effect tables + recommended config) to the experiment's GCS prefix.
- **Test** `tests/test_doe_analyze.py`: hand-built 2-factor table → known main-effect arithmetic.

### Task 15: Orchestrator CLI
**Files:** Create `src/doe/run_doe.py`
- `uv run --group doe python -m src.doe.run_doe --kind screening [--full] [--wait] [--max-runs N] [--dry-run]`: design → launch → (optional wait → harvest → analyze). `--dry-run` prints the design + cost estimate and submits nothing. `--max-runs` caps fan-out (log what's dropped).
- **Commit** `feat: DOE framework (factors, design, launch, harvest, analyze)`.

---

## Recommended first experiment (screening)

4 factors × 2 levels, **resolution-IV `2^(4-1)` = 8 runs + 1 baseline center = 9 pipeline jobs.** Main effects are clear of two-factor interactions — the right first pass to learn *which* factors move `tool_use_quality` / `final_response_match` and the cost-quality frontier before spending on a full factorial (16+).

**Cost caveat (must surface):** model_tier and prompt_variant are `engine_env`, so **8 of 9 runs deploy a fresh Agent Engine** + traffic + simulated-eval turns — the heaviest path. `run_doe.py` defaults to `--dry-run`; real fan-out is opt-in. Validate with a **2-run cheap smoke** (`--max-runs 2`, quick fidelity) before the full screening.

---

## Verification

**Offline (PR gate, no GCP):** `uv run --group doe --group pipelines pytest -q` — config overrides, boundary env, prompt toggle, deploy env_vars, pipeline compile, design shape, harvest-vs-fixture, analyze math. Full suite stays green (202 + new).

**On GCP (staged, cheapest first):**
1. `python -m src.doe.run_doe --kind screening --dry-run` → prints the 9-point design + cost estimate, submits nothing.
2. `--max-runs 2 --wait` → 2 jobs succeed, 2 temp engines auto-cleaned (none linger), tidy CSV + `report.md` land under `gs://{bucket}/eval-results/doe/{experiment_id}/`.
3. Full screening `--kind screening --wait` → 9 jobs `PIPELINE_STATE_SUCCEEDED`, harvested table complete, main-effects report identifies the highest-leverage factor for the weak metrics.

**Success =** every design point maps to a harvested row; effect report ranks factors; no stray Agent Engines; the framework re-runs with a different `--kind`/factor set without code changes.

---

## Risks & decisions

- **Import-time env caching:** subprocess-per-design-point (fresh interpreter) is the deliberate fix — do **not** try to `reload` config in one process for live runs.
- **Baseline-prompt fidelity:** recovered from `366013c^`; if a recovered prompt no longer matches current tool signatures, note it in the variant and keep gepa as default.
- **`aggressive_savings` boundaries** are a starting guess; the screening result informs the real values.
- **Least-privilege SA** (carried from the pipeline work): the fan-out runs under the same `compute@` SA — scope down before non-workshop use.
