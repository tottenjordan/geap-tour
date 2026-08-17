# Orchestrate GEAP Evals with Vertex Managed Pipelines — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: use superpowers:executing-plans to implement this plan task-by-task.
> On execution, copy this file to `docs/plans/2026-08-05-vertex-eval-pipeline.md` (the writing-plans skill's canonical location); it lives in the plan-scratch path only because plan mode restricts edits.

**Goal:** Run the existing GEAP eval suite (deploy → traffic → batch + simulated + complexity → monitor verify → report) as a single **Vertex AI Managed Pipeline** (KFP v2) instead of a GitHub Actions job graph, submitted manually on demand.

**Architecture:** A KFP v2 pipeline whose components are thin wrappers around the *existing* functions in `src/eval/*` and `src/deploy/deploy_agents.py`, all running from one container image in Artifact Registry. `run_all_evals.py` is the reference DAG. The pipeline is compiled to a spec and submitted as a `PipelineJob` (manual only — no scheduler). Work runs on Vertex/GCP compute, not on a CI runner.

**Tech Stack:** `kfp>=2` (author/compile), `google-cloud-aiplatform` (submit `PipelineJob`, already a dep), Vertex AI GenAI Eval + Agent Engine SDK (already used), Artifact Registry (Docker), hatchling (already the build backend), GCS staging bucket (`geap-tour-staging-v2`, already provisioned).

---

## Context

Today the "eval pipeline" is a **GitHub Actions** workflow (`.github/workflows/eval_pipeline.yaml` + `eval_ci.yaml`) that orchestrates `uv run python -m src.eval.*` steps on GitHub-hosted runners; the runners authenticate to GCP via WIF and call Vertex AI. There is **no** Vertex Pipelines / KFP / Artifact Registry / scheduler / container-build infra in the repo today (confirmed by exploration). The user wants the orchestration itself to move onto **Vertex Managed Pipelines** so the eval DAG runs, scales, and is observable on GCP (Vertex Pipelines UI, lineage, artifacts) rather than in CI.

**Decisions locked with the user:**
- **Trigger:** manual submit only (a `submit.py` CLI; optionally a thin `gh workflow run` that just submits). No `PipelineJobSchedule`.
- **Scope:** full parity with `run_all_evals.py` — deploy → traffic → (batch ‖ simulated ‖ complexity) → monitor-verify → report.
- **Agent under test:** parameterized — if `agent_id` param is empty, a deploy component creates a temporary Agent Engine and an exit handler deletes it; otherwise reuse the provided engine ID (no deploy, no delete).

This does **not** remove the fast PR test gate (`tests.yaml`) — that stays. It optionally retires the two GH eval workflows (see Task 8).

---

## Reference architecture

```
                         submit.py  (manual: PipelineJob.submit, SA=compute@…, root=gs://…/pipeline-root)
                              │  parameter_values + per-task env
                              ▼
        ┌──────────────────────────  eval_pipeline (KFP v2)  ──────────────────────────┐
        │                                                                               │
        │   dsl.ExitHandler(cleanup)  ← always runs; deletes engine iff deployed_fresh  │
        │   ┌───────────────────────────────────────────────────────────────────────┐ │
        │   │  resolve-agent ──► generate-traffic ──►  ┌── batch-eval ──┐             │ │
        │   │  (deploy fresh   (skip via param;        ├── simulated ───┤──► report   │ │
        │   │   OR passthrough) sleep for ingest)      └── complexity ──┘    (md+json │ │
        │   │        │                                       │                → GCS)   │ │
        │   │        └────────────► monitor-verify ◄─────────┘ (after batch)          │ │
        │   └───────────────────────────────────────────────────────────────────────┘ │
        └───────────────────────────────────────────────────────────────────────────────┘
                                          │
              All components run FROM one image:  us-central1-docker.pkg.dev/hybrid-vertex/geap-eval/eval-runner:<tag>
              Artifacts (JSON/report.md) → gs://geap-tour-staging-v2/{pipeline-root, eval-results}/…
```

**Component ↔ existing code (reuse, do not rewrite):**

| Component | Wraps (file:function) | Key output |
|---|---|---|
| `resolve-agent` | `src/deploy/deploy_agents.py:deploy_agent()` (+ `_resolve_agent_resource_name` pattern from `run_all_evals.py:25`) | `agent_resource: str`, `deployed_fresh: bool` |
| `generate-traffic` | `src/traffic/generate_traffic.py:generate_traffic(agent, count)` | log artifact |
| `batch-eval` | `src/eval/multi_agent_batch_eval.py:run_multi_agent_batch_eval(agent_id, score_threshold, output_path)` | `batch_results.json`, `Metrics`, `passed: bool` |
| `simulated-eval` | `src/eval/simulated_eval.py:run_simulated_eval(...)` for `coordinator_agent`,`travel_agent` | `simulation_results.json`, `passed: bool` |
| `complexity-eval` | `src/eval/complexity_metrics.py:run_complexity_accuracy_eval / run_cost_efficiency_eval` + `agent_eval_configs.ROUTER_EVAL_CASES` | `complexity_eval.json`, `Metrics` |
| `monitor-verify` | `src/eval/verify_monitors.py:verify_monitor_results / generate_markdown_report` | `monitor_status.json` |
| `report` | `src/eval/run_all_evals.py:_generate_report()` logic (lift into shared helper) | `report.md`, `full_results.json` → GCS |
| `cleanup` (exit handler) | `vertexai.agent_engines.delete(resource)` (as in `eval_ci.yaml` cleanup step) | — |

---

## Prerequisites (mostly already done)

- GCS staging bucket `geap-tour-staging-v2` — **exists** (repo var). Pipeline root: `gs://geap-tour-staging-v2/pipeline-root`.
- Pipeline runtime service account — reuse `934903580331-compute@developer.gserviceaccount.com` (already `roles/owner`; note least-privilege as a follow-up: `roles/aiplatform.user`, `roles/storage.objectAdmin` on the bucket, `roles/iam.serviceAccountUser`).
- **New:** Artifact Registry Docker repo. One-time:
  ```bash
  gcloud artifacts repositories create geap-eval \
      --project=hybrid-vertex --location=us-central1 --repository-format=docker
  ```

---

## Task 1: Add pipeline authoring deps (compile/submit only — NOT in the runtime image)

**Files:** Modify `pyproject.toml`

**Step 1:** Add a PEP 735 group (keeps `kfp` out of the eval container, which only needs `src` + its runtime deps):
```bash
uv add --group pipelines "kfp>=2.7"
# google-cloud-aiplatform is already a project dependency (used for PipelineJob.submit)
```
**Step 2:** `uv sync --group pipelines` and confirm `python -c "import kfp; print(kfp.__version__)"`.
**Step 3:** Commit: `chore: add kfp pipelines dependency group`.

---

## Task 2: Container image for all components

**Files:**
- Create: `docker/eval/Dockerfile`
- Create: `scripts/build_eval_image.sh`

**Step 1:** `docker/eval/Dockerfile` — install the project as a wheel (hatchling packages `src/`, including the `src/eval/evalsets/*.json` and `scenarios/*.json` data files needed by the eval cases):
```dockerfile
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 GOOGLE_GENAI_USE_VERTEXAI=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
# sane non-secret defaults; deployment-specific values are injected per-task at run time
ENV GCP_PROJECT_ID=hybrid-vertex GCP_REGION=us-central1 \
    GCP_STAGING_BUCKET=geap-tour-staging-v2 EVAL_OUTPUT_DIR=/tmp/eval_outputs
```
> Pin Python **3.12** to match `requires-python`. (The current GH eval jobs run 3.11 — a latent inconsistency; standardize on 3.12 here.)

**Step 2:** `scripts/build_eval_image.sh` — build+push via Cloud Build (no local Docker needed):
```bash
#!/usr/bin/env bash
set -euo pipefail
PROJECT=${GCP_PROJECT_ID:-hybrid-vertex}; REGION=${GCP_REGION:-us-central1}
TAG=${1:-latest}
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/geap-eval/eval-runner:${TAG}"
gcloud builds submit --project="$PROJECT" --tag "$IMAGE" \
    --config=/dev/stdin <<EOF
steps:
- name: gcr.io/cloud-builders/docker
  args: ['build','-f','docker/eval/Dockerfile','-t','${IMAGE}','.']
images: ['${IMAGE}']
EOF
echo "$IMAGE"
```
**Step 3:** Build once: `bash scripts/build_eval_image.sh v1` → note the image URI.
**Step 4:** Commit: `feat: add eval-runner container image + build script`.

---

## Task 3: Pipeline components (thin wrappers over existing functions)

**Files:**
- Create: `src/pipelines/__init__.py`
- Create: `src/pipelines/components.py`
- Test: `tests/test_pipeline.py`

**Design:** function-based `@dsl.component(base_image=IMAGE)` components. Each imports the existing function, writes its JSON to the KFP `Output[Artifact]` path, emits `Output[Metrics]` where scores exist, and returns a pass/fail via `NamedTuple`. **Critical gotcha:** `src/config.py` reads env vars *at import time* (`load_dotenv()` + `os.environ.get`). The container has no `.env`, so deployment-specific vars (`SEARCH_MCP_SERVER`, `BOOKING_MCP_SERVER`, `EXPENSE_MCP_SERVER`, `AGENT_ENGINE_ID`, `ROUTER_ENGINE_ID`) must be present in the process env **before** `src.*` is imported. These are set per-task in Task 4 via `.set_env_variable(...)`; components must therefore do their `from src... import ...` *inside* the function body (KFP function components already inline imports), so the env is in place first. **This is exactly the gap that made the GH `simulated-eval` fail with `env[17-19] Required field is not set`.**

**Step 1:** Write `tests/test_pipeline.py` first (offline, no GCP) — assert each component is a KFP component and the DAG compiles:
```python
def test_components_are_kfp():
    from src.pipelines import components as c

    for name in (
        "resolve_agent",
        "generate_traffic",
        "batch_eval",
        "simulated_eval",
        "complexity_eval",
        "monitor_verify",
        "report",
        "cleanup",
    ):
        comp = getattr(c, name)
        assert hasattr(comp, "component_spec"), f"{name} is not a KFP component"
```
**Step 2:** Run: `uv run pytest tests/test_pipeline.py -v` → FAIL (module missing).
**Step 3:** Implement `src/pipelines/components.py`. Representative components (others follow the same shape):
```python
from typing import NamedTuple
from kfp import dsl

IMAGE = "us-central1-docker.pkg.dev/hybrid-vertex/geap-eval/eval-runner:v1"


@dsl.component(base_image=IMAGE)
def resolve_agent(agent_id: str, agent_module: str) -> NamedTuple(
    "Out", [("agent_resource", str), ("deployed_fresh", bool)]
):
    from src.config import GCP_PROJECT_ID, GCP_REGION

    if agent_id:
        res = (
            agent_id
            if agent_id.startswith("projects/")
            else f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/reasoningEngines/{agent_id}"
        )
        return (res, False)
    import importlib
    from src.deploy.deploy_agents import deploy_agent

    mod = importlib.import_module(f"src.agents.{agent_module}")
    res = deploy_agent(getattr(mod, agent_module))
    return (res, True)


@dsl.component(base_image=IMAGE)
def batch_eval(
    agent_resource: str,
    threshold: float,
    results: dsl.Output[dsl.Artifact],
    metrics: dsl.Output[dsl.Metrics],
) -> bool:
    from src.eval.multi_agent_batch_eval import run_multi_agent_batch_eval

    r = run_multi_agent_batch_eval(
        agent_id=agent_resource, score_threshold=threshold, output_path=results.path
    )
    for agent, ar in (r.get("agents") or {}).items():
        for m, mv in (ar.get("metrics") or {}).items():
            metrics.log_metric(f"{agent}.{m}", mv["score"])
    return bool(r.get("all_passed"))


@dsl.component(base_image=IMAGE)
def cleanup(agent_resource: str, deployed_fresh: bool):
    if not deployed_fresh:
        return
    import vertexai
    from vertexai import agent_engines
    from src.config import GCP_PROJECT_ID, GCP_REGION

    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    try:
        agent_engines.delete(agent_resource, force=True)
    except Exception as e:
        print(f"cleanup skipped: {e}")
```
Implement `generate_traffic`, `simulated_eval` (loop coordinator+travel, write `simulation_results.json`), `complexity_eval` (asyncio.run the two evals, log accuracy metric), `monitor_verify`, and `report` similarly — `report` reuses the `_generate_report` logic (Task 5 refactor) fed the upstream artifacts, then copies `report.md`/`full_results.json` to `gs://…/eval-results/<run>/`.
**Step 4:** Run `uv run pytest tests/test_pipeline.py -v` → PASS.
**Step 5:** Commit: `feat: add KFP components wrapping eval functions`.

---

## Task 4: Pipeline definition (DAG, params, per-task env, exit handler)

**Files:**
- Create: `src/pipelines/eval_pipeline.py`
- Test: extend `tests/test_pipeline.py`

**Step 1:** Add a compile test (offline):
```python
def test_pipeline_compiles(tmp_path):
    from kfp import compiler
    from src.pipelines.eval_pipeline import eval_pipeline

    out = tmp_path / "pipeline.json"
    compiler.Compiler().compile(eval_pipeline, str(out))
    assert out.exists() and out.stat().st_size > 0
```
**Step 2:** Run → FAIL.
**Step 3:** Implement `eval_pipeline.py`:
```python
from kfp import dsl
from src.pipelines import components as c

# Deployment-specific env applied to EVERY task (import-time config gap fix).
_RUNTIME_ENV = {  # values come from pipeline params → set below
    "SEARCH_MCP_SERVER": None,
    "BOOKING_MCP_SERVER": None,
    "EXPENSE_MCP_SERVER": None,
    "AGENT_ENGINE_ID": None,
    "ROUTER_ENGINE_ID": None,
}


def _wire(task, env: dict):
    for k, v in env.items():
        task.set_env_variable(k, v)
    return task


@dsl.pipeline(name="geap-eval-pipeline", pipeline_root="gs://geap-tour-staging-v2/pipeline-root")
def eval_pipeline(
    agent_id: str = "",
    agent_module: str = "coordinator_agent",
    threshold: float = 3.0,
    skip_traffic: bool = False,
    traffic_count: int = 2,
    scenario_count: int = 5,
    max_turns: int = 3,
    search_mcp: str = "",
    booking_mcp: str = "",
    expense_mcp: str = "",
    router_engine_id: str = "",
):
    env = {
        "SEARCH_MCP_SERVER": search_mcp,
        "BOOKING_MCP_SERVER": booking_mcp,
        "EXPENSE_MCP_SERVER": expense_mcp,
        "ROUTER_ENGINE_ID": router_engine_id,
    }
    resolve = _wire(c.resolve_agent(agent_id=agent_id, agent_module=agent_module), env)
    with dsl.ExitHandler(
        _wire(
            c.cleanup(
                agent_resource=resolve.outputs["agent_resource"],
                deployed_fresh=resolve.outputs["deployed_fresh"],
            ),
            env,
        )
    ):
        agent_res = resolve.outputs["agent_resource"]
        with dsl.If(skip_traffic == False):
            traffic = _wire(c.generate_traffic(agent_resource=agent_res, count=traffic_count), env)
        batch = _wire(c.batch_eval(agent_resource=agent_res, threshold=threshold), env)
        sim = _wire(
            c.simulated_eval(
                agent_resource=agent_res,
                threshold=threshold,
                scenario_count=scenario_count,
                max_turns=max_turns,
            ),
            env,
        )
        comp = _wire(c.complexity_eval(), env)
        mon = _wire(c.monitor_verify(agent_resource=agent_res), env).after(batch)
        _wire(
            c.report(
                batch_results=batch.outputs["results"],
                sim_results=sim.outputs["results"],
                complexity_results=comp.outputs["results"],
                monitor_results=mon.outputs["results"],
            ),
            env,
        )
```
> Notes: `dsl.ExitHandler` guarantees `cleanup` runs even on failure; `cleanup` no-ops when `deployed_fresh` is false. `dsl.If` gates traffic. `.after(batch)` reproduces `run_all_evals`'s monitor-after-batch ordering; batch/simulated/complexity otherwise run in parallel.
**Step 4:** Run compile test → PASS.
**Step 5:** Commit: `feat: add full-parity KFP eval pipeline`.

---

## Task 5: Extract the report builder for reuse (DRY)

**Files:** Modify `src/eval/run_all_evals.py`

**Step 1:** Refactor the body of `_generate_report()` (`run_all_evals.py:192-275`) into a pure `build_report(results: dict) -> str` that returns markdown and does no file I/O; have `_generate_report` call it and keep writing `report.md`/`full_results.json`. The `report` component (Task 3) imports `build_report` and assembles `results` from the upstream artifact JSONs.
**Step 2:** Add `tests/test_report_builder.py` asserting `build_report({...minimal...})` contains the expected section headers. Run → iterate to PASS.
**Step 3:** Commit: `refactor: extract build_report for pipeline reuse`.

---

## Task 6: Submit CLI (manual trigger)

**Files:** Create `src/pipelines/submit.py`

**Step 1:** Implement compile-then-submit:
```python
"""Compile and submit the eval pipeline. Usage: uv run python -m src.pipelines.submit [--agent-id ID]"""

import argparse
from kfp import compiler
from google.cloud import aiplatform
from src.config import (
    GCP_PROJECT_ID,
    GCP_REGION,
    GCP_STAGING_BUCKET,
    SEARCH_MCP_SERVER,
    BOOKING_MCP_SERVER,
    EXPENSE_MCP_SERVER,
    ROUTER_ENGINE_ID,
)
from src.pipelines.eval_pipeline import eval_pipeline


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--agent-id", default="")  # empty → deploy fresh
    p.add_argument("--agent-module", default="coordinator_agent")
    p.add_argument("--threshold", type=float, default=3.0)
    p.add_argument("--skip-traffic", action="store_true")
    p.add_argument(
        "--service-account", default="934903580331-compute@developer.gserviceaccount.com"
    )
    a = p.parse_args()
    compiler.Compiler().compile(eval_pipeline, "eval_pipeline.json")
    aiplatform.init(
        project=GCP_PROJECT_ID, location=GCP_REGION, staging_bucket=f"gs://{GCP_STAGING_BUCKET}"
    )
    job = aiplatform.PipelineJob(
        display_name="geap-eval",
        template_path="eval_pipeline.json",
        pipeline_root=f"gs://{GCP_STAGING_BUCKET}/pipeline-root",
        parameter_values={
            "agent_id": a.agent_id,
            "agent_module": a.agent_module,
            "threshold": a.threshold,
            "skip_traffic": a.skip_traffic,
            "search_mcp": SEARCH_MCP_SERVER,
            "booking_mcp": BOOKING_MCP_SERVER,
            "expense_mcp": EXPENSE_MCP_SERVER,
            "router_engine_id": ROUTER_ENGINE_ID,
        },
    )
    job.submit(service_account=a.service_account)  # non-blocking; prints console URL
    print(job._dashboard_uri())


if __name__ == "__main__":
    main()
```
**Step 2:** Commit: `feat: add manual PipelineJob submit CLI`.

---

## Task 7 (optional): Thin GitHub Action to submit

**Files:** Create `.github/workflows/eval_vertex.yaml` (`workflow_dispatch` only)

One job: WIF auth (existing `vars.WIF_PROVIDER`) → `uv sync --group pipelines` → `uv run python -m src.pipelines.submit`. The runner only *submits*; all eval compute runs on Vertex. Guard with `if: ${{ vars.WIF_PROVIDER != '' }}` (same pattern as current eval workflows). Commit: `ci: add manual Vertex eval-pipeline submit workflow`.

---

## Task 8: Retire / repoint the old eval workflows + docs

**Files:** Modify or delete `.github/workflows/eval_pipeline.yaml`, `.github/workflows/eval_ci.yaml`; create `docs/notes/vertex-eval-pipeline.md`; update `CLAUDE.md` commands section.

- Once Task 6/7 works, the two GH eval workflows are redundant. Either delete them or leave as-is (manual). **Recommend:** delete `eval_pipeline.yaml`/`eval_ci.yaml` to avoid two orchestrators; keep `tests.yaml` (PR gate). Decide with reviewer.
- Add a `docs/notes/` entry per the session-notes convention: image URI, submit command, the import-time-env gotcha, and the least-privilege-SA follow-up.
- Add to `CLAUDE.md`: `uv run python -m src.pipelines.submit` and `bash scripts/build_eval_image.sh <tag>`.
- Commit: `docs: document Vertex eval pipeline; retire GH eval workflows`.

---

## Verification

**Offline (in the PR test gate — no GCP):**
```bash
uv run pytest tests/test_pipeline.py tests/test_report_builder.py -v   # components exist, pipeline compiles, report builder
uv run pytest                                                          # full suite still green (198+)
```

**On GCP (manual smoke — cheapest path first):**
1. One-time: create Artifact Registry repo (Prerequisites) and `bash scripts/build_eval_image.sh v1`.
2. Reuse-existing-engine, skip traffic (fastest, no deploy):
   ```bash
   uv run python -m src.pipelines.submit --agent-id "$AGENT_ENGINE_ID" --skip-traffic
   ```
   Open the printed Vertex Pipelines URL; confirm `resolve-agent → batch/simulated/complexity → monitor → report` succeed and `cleanup` no-ops (deployed_fresh=false).
3. Full parity with fresh deploy:
   ```bash
   uv run python -m src.pipelines.submit --agent-module coordinator_agent
   ```
   Confirm a temp Agent Engine is created, all phases run, `report.md`/`full_results.json` land in `gs://geap-tour-staging-v2/eval-results/…`, and the exit-handler `cleanup` deletes the temp engine (verify none linger: `gcloud ai reasoning-engines list --region=us-central1`).

**Success =** pipeline `PipelineState.PIPELINE_STATE_SUCCEEDED`, artifacts + metrics visible in the Vertex Pipelines UI, report in GCS, no stray Agent Engine after a fresh-deploy run.

---

## Key gotchas (carry into execution)

1. **Import-time env reads** — `src/config.py` resolves env at import; set `*_MCP_SERVER` / engine IDs via `.set_env_variable()` on every task and keep `from src...` imports inside component bodies. This is the root cause of the earlier `simulated-eval` `env[17-19] Required field is not set` failure.
2. **Data files in the wheel** — evalset/scenario JSONs live under `src/eval/evalsets|scenarios`; hatchling includes them because it packages the `src` dir. Verify they're present in the image (`docker run … python -c "import importlib.resources"` or a quick `ls`) before the first real run.
3. **Python version** — image is 3.12 (matches `requires-python`); the legacy GH eval jobs used 3.11. Standardize on 3.12.
4. **`cleanup` must be idempotent & gated** — only delete when `deployed_fresh` is true; swallow "already deleted" errors so the exit handler never fails the run.
5. **Cost** — fresh-deploy + traffic + simulated eval is the heaviest path (deploys a real Agent Engine, LLM user-simulator turns). Default smoke tests to reuse-engine + `--skip-traffic`.
6. **Least privilege (follow-up)** — the pipeline SA currently has `roles/owner`; scope to `aiplatform.user` + bucket `storage.objectAdmin` + `iam.serviceAccountUser` before any non-workshop use.
