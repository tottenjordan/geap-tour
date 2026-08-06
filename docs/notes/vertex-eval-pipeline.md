# Vertex Managed Pipeline for evals

The eval suite (deploy → traffic → batch ‖ simulated ‖ complexity → monitor →
report) runs as a single **Vertex AI Managed Pipeline** (KFP v2), submitted
manually. It replaces the GitHub Actions eval job graph; the compute runs on
Vertex, observable in the Vertex Pipelines UI with lineage + artifacts.

## Layout

- `src/pipelines/components.py` — 8 `@dsl.component`s, each a thin wrapper over an
  existing `src/eval/*` (or `deploy_agents`) function.
- `src/pipelines/eval_pipeline.py` — the DAG (params, per-task env, exit-handler
  cleanup, `dsl.If` traffic gate).
- `src/pipelines/submit.py` — compile + submit a `PipelineJob` (manual trigger).
- `docker/eval/Dockerfile` + `scripts/build_eval_image.sh` — the single runner
  image backing every component.
- `.github/workflows/eval_vertex.yaml` — thin `workflow_dispatch` that only
  submits (all eval compute runs on Vertex).

## One-time setup (GCP)

```bash
# 1. Artifact Registry Docker repo
gcloud artifacts repositories create geap-eval \
    --project=hybrid-vertex --location=us-central1 --repository-format=docker

# 2. Build + push the runner image (note the printed URI)
bash scripts/build_eval_image.sh v1
```

The image URI is pinned in `src/pipelines/components.py` as
`IMAGE = us-central1-docker.pkg.dev/hybrid-vertex/geap-eval/eval-runner:v1`.
Rebuild with a new tag and bump `IMAGE` to update.

## Submit

```bash
# Reuse an existing engine, skip traffic (fastest, no deploy)
uv run python -m src.pipelines.submit --agent-id "$AGENT_ENGINE_ID" --skip-traffic

# Full parity with a fresh temp deploy (auto-cleaned by the exit handler)
uv run python -m src.pipelines.submit --agent-module coordinator_agent
```

## KFP gotchas discovered during implementation

These three plan assumptions were wrong against `kfp` 2.17 and were reworked:

1. **`set_env_variable` cannot take a pipeline parameter** — it needs a plain
   string (`EnvVar(name, value)` rejects a `PipelineParameterChannel`, failing at
   compile). So deployment env (`SEARCH_MCP_SERVER`, `BOOKING_MCP_SERVER`,
   `EXPENSE_MCP_SERVER`, `ROUTER_ENGINE_ID`) is read from `src.config` at
   `eval_pipeline.py` import time and baked onto every task as **static** env via
   `_wire()`. `submit.py` recompiles each run, so the values track the current
   `.env`.

2. **`src.config` reads env at import time** — every `from src... import` inside a
   component body runs AFTER the injected env is present. Components must keep all
   `src.*` imports inside the function body (never module-level). This is the same
   gap that made the old GH `simulated-eval` fail with `env[17-19] Required field
   is not set`.

3. **ExitHandler exit tasks cannot depend on other tasks**
   (`ValueError: exit_task cannot depend on any other tasks`). So `cleanup` works
   from pipeline params only: `submit.py` generates a unique
   `temp_display_name` for fresh deploys, `resolve_agent` deploys the temp engine
   under that name, and `cleanup` lists engines and deletes the one matching it.
   Reuse runs (`--agent-id` set) skip cleanup entirely.

Also: compile to `.yaml`, not `.json` (JSON compile is deprecated in kfp 2.17).

## Follow-ups

- **Least privilege** — the pipeline SA
  (`934903580331-compute@developer.gserviceaccount.com`) currently has
  `roles/owner`. Before any non-workshop use, scope to `roles/aiplatform.user` +
  bucket `roles/storage.objectAdmin` + `roles/iam.serviceAccountUser`.
- **Data files in the image** — evalset/scenario JSONs ship because hatchling
  packages `src/`. Verify they're present in the image before the first real run
  (`docker run … ls src/eval/evalsets`).
