# Resource Labels + AGENT_ENGINE_ID Durable Fix Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.
> On execution, copy this file to `docs/plans/2026-08-12-resource-labels-and-engine-id-fix.md`.

**Goal:** Stop the `AGENT_ENGINE_ID` drift for good (durable `.env` write + runtime-scoped session/memory builders) and stamp a default `{"solution": "geap-tour"}` resource label onto every GCP resource we create — in code/scripts for the future and retro-actively on the ~14 assets already provisioned in `hybrid-vertex` — then fail-fast smoke the coordinator end-to-end.

**Architecture:** One shared label constant in `src/config.py` fed into every Python creation site; a hardcoded sibling literal in each shell/gcloud site (scripts can't import Python). The durable engine-id fix hooks the coordinator's fresh-create path in `deploy_agents.py` to also rewrite `AGENT_ENGINE_ID`. The already-written runtime-builder fix is committed as-is. Retro-labeling is a single idempotent patch script. Everything ships TDD with offline tests (fakes/monkeypatch, no live GCP).

**Tech Stack:** Vertex AI Agent Engine (`vertexai.Client().agent_engines`), `google-cloud-monitoring` (AlertPolicy `user_labels`, Dashboard `labels`), `google-cloud-bigquery` / `bq`, `gcloud run`/`storage`, Model Armor REST, `vertexai._genai` evals + `aiplatform.PipelineJob`, pytest.

---

## Context

**Why now.** A live customer demo runs against the coordinator + router deployed in `hybrid-vertex`. Two problems surfaced this session:

1. **`AGENT_ENGINE_ID` drift.** `config.AGENT_ENGINE_ID` is a general-purpose "default engine" pointer that `deploy_agents.py` never rewrites on deploy. It leaked into two places that broke:
   - **Server-side (the live bug):** the memory/session service *builders* baked `config.AGENT_ENGINE_ID` (a stale/other engine, `4709107696450666496`) instead of the coordinator's own id (`4181778621234413568`), so `create_session` failed at runtime with "Failed to create session". Proven from the deployed engine's `gca_resource` spec.
   - **Client-side:** `coordinator_a2a_url()` and the `engine_id` metric label default to the same stale value.
   The runtime-builder fix (`_runtime_engine_id()` preferring the runtime-injected `GOOGLE_CLOUD_AGENT_ENGINE_ID`) is **already written + tested but uncommitted** in `deploy_agents.py:104-149` / `tests/test_memory_wiring.py:136-170`. It needs committing, plus a durable `.env` write so the client-side pointer stops drifting too.

2. **No resource labels.** Nothing we create carries an ownership/cost label, so demo assets can't be filtered or attributed. The user wants a default `{"solution": "geap-tour"}` on everything we can, applied both to future creates (code/scripts) and retro-actively to the assets already created.

**Intended outcome.** `.env` points `AGENT_ENGINE_ID` at the coordinator and stays correct across future deploys; every labelable resource we create is tagged `solution=geap-tour`; the 14 already-provisioned assets are patched; and a coordinator smoke run proves the session bug is dead.

**Decisions locked (via AskUserQuestion):**
- **Label scope = confirmed + best-effort armor.** Apply to all SDK-confirmed resources + Model Armor templates (best-effort) + DNS zone. **Skip** preview/unknown REST surfaces (online evaluators, Agent Registry, Agent Gateways, SGP/authz) to avoid a 400 from an unsupported field.
- **Retro-label = one idempotent patch script** (`scripts/apply_resource_labels.py`), no engine re-upload.

### Grounding facts (from exploration — do not re-derive)

- **Label field names (verified in venv):** Agent Engine `config["labels"]` (`dict[str,str]`); AlertPolicy `user_labels`; Dashboard `labels`; BigQuery `bq mk --label k:v` (colon) / `Dataset.labels`; Cloud Run `--labels k=v` (equals, comma-sep); GCS bucket `--labels=k=v`; `create_evaluation_run(labels=...)`; `PipelineJob(labels=...)`; DNS zone `--labels`. **No labels:** logging sink, WIP pools, App Hub (uses `--attributes`), VPC/subnet. **Preview/unknown (skip):** online evaluators, Agent Registry, Agent Gateways, SGP/authz.
- **Creation sites:** `deploy_agents.py:238-244` (`_build_config` config dict) + `:279/:294` create/update; `quality_alerts.py:36-49`; `dashboard.py:94-97`; `metrics.py:141-146` (`_default_labels` — metric dimensions, cheap to extend); `deploy_mcp_servers.py:20-28`; `scripts/deploy_all.sh:64` (bucket) / `:74` (Cloud Run); `scripts/setup_logging_sink.sh:22` (bq mk); `scripts/setup_model_armor.sh:40/64` (REST body); eval-run sites `batch_eval.py:448`, `cross_model_experiment.py:92`, `simulated_eval.py:118`, `multi_agent_batch_eval.py:110`; `submit.py:64` / `submit_optimize.py:54` (PipelineJob); `scripts/setup_governance_policies.sh:378` (DNS zone).
- **Durable-fix hook:** `deploy_agents.py:409` (`_update_env_file(entry["env_var"], resource_name)` in the fresh-create branch). Add a coordinator-guarded `AGENT_ENGINE_ID` write right after. The `--update` branch (`:400-405`) intentionally writes no `.env`.
- **Test conventions:** offline only — `types.SimpleNamespace` fake agents, `monkeypatch` for GCP clients/env, redirect `da.ENV_FILE` to `tmp_path` for `.env`-write tests. Existing patterns in `tests/test_deploy_agents.py` (calls `_build_config` directly, asserts on returned dict) and `tests/test_memory_wiring.py`.
- **jt1 engine ids:** coordinator `4181778621234413568`, router `6134089059699523584`, lite `2740626740475854848`, flash `7802672721640292352`, pro `1692413927205371904`, sonnet `6945862892533055488`, opus `7735118727229734912`. Stale `AGENT_ENGINE_ID=4709107696450666496`. Monitoring assets: alert policies `17880394120747062646`, `13647462424032699974`, `8552465886428325732`, `8258683741623916435`; dashboard `9c89339d-7b5e-4b17-a69c-91e5b45cb2df`; BQ dataset `geap_workshop_logs`; Model Armor templates `geap-workshop-prompt` / `geap-workshop-response`.
- **CODE_STANDARDS.md:9** — never add `Co-Authored-By` trailers to commits/PRs.

---

## Task 0 — Commit the already-written runtime-builder fix

The `_runtime_engine_id()` change (`deploy_agents.py:104-149`) + its tests (`test_memory_wiring.py:136-170`) are written and passing but uncommitted. Land them first so the smoke redeploy carries the session-scope fix, and so later tasks build on a clean tree.

**Step 1:** Confirm the working tree contains only these two files' runtime-fix changes: `git diff --stat`.
**Step 2:** Run the suite: `uv run pytest tests/test_memory_wiring.py -q` → expect green (24 tests).
**Step 3:** Commit:
```bash
git add src/deploy/deploy_agents.py tests/test_memory_wiring.py
git commit -m "fix: scope session/memory services to the engine's own runtime id"
```

---

## Task 1 — Shared label constant in `src/config.py`

**Files:** Modify `src/config.py`; Test `tests/test_config_labels.py` (create).

**Step 1 — failing test:**
```python
# tests/test_config_labels.py
import src.config as cfg

def test_resource_labels_default():
    assert cfg.RESOURCE_LABELS == {"solution": "geap-tour"}

def test_resource_labels_gcloud_format():
    assert cfg.resource_labels_gcloud() == "solution=geap-tour"

def test_resource_labels_bq_flags():
    assert cfg.resource_labels_bq_flags() == ["--label", "solution:geap-tour"]
```
**Step 2:** `uv run pytest tests/test_config_labels.py -q` → FAIL (no `RESOURCE_LABELS`).
**Step 3 — implement** (add near the top-level constants in `src/config.py`, after the GCP block ~line 15):
```python
# Default resource label stamped onto every GCP resource we create, so demo
# assets are filterable/attributable. Override the value with SOLUTION_LABEL.
RESOURCE_LABELS = {"solution": os.environ.get("SOLUTION_LABEL", "geap-tour")}

def resource_labels_gcloud() -> str:
    """RESOURCE_LABELS as a gcloud --labels value: comma-joined key=value."""
    return ",".join(f"{k}={v}" for k, v in RESOURCE_LABELS.items())

def resource_labels_bq_flags() -> list[str]:
    """RESOURCE_LABELS as repeated `bq` --label key:value flags."""
    flags = []
    for k, v in RESOURCE_LABELS.items():
        flags += ["--label", f"{k}:{v}"]
    return flags
```
**Step 4:** `uv run pytest tests/test_config_labels.py -q` → PASS.
**Step 5:** Commit `feat: add shared RESOURCE_LABELS constant + gcloud/bq formatters`.

---

## Task 2 — Durable `AGENT_ENGINE_ID` write on coordinator create

**Files:** Modify `src/deploy/deploy_agents.py:406-409`; Test add to `tests/test_deploy_agents.py`.

**Step 1 — failing test** (redirect `ENV_FILE` to tmp, fake client, deploy coordinator, assert `.env` got both keys):
```python
def test_coordinator_create_writes_agent_engine_id(monkeypatch, tmp_path):
    import types
    import src.deploy.deploy_agents as da
    env = tmp_path / ".env"
    monkeypatch.setattr(da, "ENV_FILE", str(env))
    monkeypatch.setattr(da, "_get_client", lambda: _FakeClient())  # create → .../reasoningEngines/999
    monkeypatch.setattr(da.vertexai, "init", lambda **k: None)
    monkeypatch.setitem(da.AGENT_SETS["coordinator"], "loader",
                        lambda: types.SimpleNamespace(name="coordinator_agent", tools=[]))
    da.run_deploy(agent_set="coordinator", update=False)
    text = env.read_text()
    assert "COORDINATOR_AGENT_ID=999" in text
    assert "AGENT_ENGINE_ID=999" in text

def test_router_create_does_not_touch_agent_engine_id(monkeypatch, tmp_path):
    # same harness for "router" → assert "AGENT_ENGINE_ID=" NOT written
```
Reuse the `_FakeClient`/`_FakeAgentEngines` pattern from `tests/test_memory_wiring.py` (create returns `projects/p/locations/us-central1/reasoningEngines/999`).
**Step 2:** Run → FAIL (only `COORDINATOR_AGENT_ID` written).
**Step 3 — implement** at `deploy_agents.py:406-409`:
```python
        else:
            resource_name = deploy_agent(agent, display_name)
            deployed[agent.name] = resource_name
            _update_env_file(entry["env_var"], resource_name)
            # Durable fix: the coordinator IS the default engine. Keep
            # AGENT_ENGINE_ID pointed at it so config-derived client-side
            # defaults (a2a url, metric labels) never drift to a stale engine.
            if name == "coordinator":
                _update_env_file("AGENT_ENGINE_ID", resource_name)
```
**Step 4:** Run → PASS.
**Step 5:** Commit `fix: repoint AGENT_ENGINE_ID to the coordinator on fresh deploy`.

---

## Task 3 — Labels on Agent Engine deploy config

**Files:** Modify `src/deploy/deploy_agents.py` (import + `_build_config` config dict ~`:238-244`); Test add to `tests/test_deploy_agents.py`.

**Step 1 — failing test:**
```python
def test_build_config_sets_resource_labels():
    from src.deploy.deploy_agents import _build_config
    import src.config as cfg
    assert _build_config(_fake_agent())["labels"] == cfg.RESOURCE_LABELS
```
**Step 2:** Run → FAIL.
**Step 3 — implement:** import `RESOURCE_LABELS` in the `from src.config import (...)` block, then add to the `config` dict:
```python
    config = {
        "staging_bucket": f"gs://{GCP_STAGING_BUCKET}",
        "requirements": REQUIREMENTS,
        "display_name": display_name or agent.name,
        "env_vars": env_vars,
        "extra_packages": ["src"],
        "labels": dict(RESOURCE_LABELS),
    }
```
**Step 4:** Run → PASS (covers both create `:279` and update `:294` — both flow through `_build_config`).
**Step 5:** Commit `feat: label deployed agent engines with solution=geap-tour`.

---

## Task 4 — Labels on alert policies + dashboard

**Files:** Modify `src/eval/quality_alerts.py:36-47`, `src/observability/dashboard.py:94-97`; Tests add to `tests/test_quality_alerts.py` (create if absent) + `tests/test_dashboard.py`.

**Step 1 — failing tests:**
```python
# quality_alerts: extract the AlertPolicy(...) construction (:36-47) into a small
# _build_policy(metric_name, threshold, channels) helper and test it (no client).
def test_alert_policy_has_resource_labels():
    from src.eval.quality_alerts import _build_policy
    import src.config as cfg
    p = _build_policy("helpfulness", 3.0, [])
    assert dict(p.user_labels) == cfg.RESOURCE_LABELS

# dashboard: build_dashboard() is already a pure function.
def test_dashboard_has_resource_labels():
    from src.observability.dashboard import build_dashboard
    import src.config as cfg
    assert dict(build_dashboard().labels) == cfg.RESOURCE_LABELS
```
**Step 2:** Run → FAIL.
**Step 3 — implement:**
- `quality_alerts.py`: extract the `monitoring_v3.AlertPolicy(...)` build into `_build_policy(...)`, add `user_labels=dict(RESOURCE_LABELS)`, have `create_quality_alert` call it. Import `RESOURCE_LABELS` from `src.config` (already imports `GCP_PROJECT_ID`).
- `dashboard.py`: add `labels=dict(RESOURCE_LABELS)` to the `dashboard_v1.Dashboard(...)` return (`:94-97`); import `RESOURCE_LABELS` (already imports `GCP_PROJECT_ID`).
**Step 4:** Run both → PASS.
**Step 5:** Commit `feat: label alert policies + dashboard with solution=geap-tour`.

---

## Task 5 — Labels on custom-metric dimensions

**Files:** Modify `src/observability/metrics.py:141-146`; Test add to `tests/test_metrics.py`.

Metric labels are time-series dimensions, not resource labels, but they're a cheap, safe surface (alerts filter on `metric.type`+`resource.type`, not these) — fold the label in so quality/traffic series are filterable by solution.

**Step 1 — failing test:** assert `_default_labels(None)` contains `"solution": "geap-tour"` (alongside existing `engine_id`/`region`).
**Step 2:** Run → FAIL.
**Step 3 — implement:** in `_default_labels`, seed `labels = {"engine_id": AGENT_ENGINE_ID, "region": GCP_REGION, **RESOURCE_LABELS}`; import `RESOURCE_LABELS`.
**Step 4:** Run → PASS (existing metric tests stay green — they assert on keys they set, not exhaustively).
**Step 5:** Commit `feat: tag custom metric series with solution label`.

---

## Task 6 — Labels on eval runs + pipeline jobs

**Files:** Modify `src/eval/batch_eval.py:448`, `src/eval/cross_model_experiment.py:92`, `src/eval/simulated_eval.py:118`, `src/eval/multi_agent_batch_eval.py:110` (`create_evaluation_run(...)`), and `src/pipelines/submit.py:64`, `src/pipelines/submit_optimize.py:54` (`PipelineJob(...)`); Test `tests/test_labels_wired.py` (static guard — these call live services).

**Step 1 — failing test:**
```python
# tests/test_labels_wired.py — static guard so future edits keep labels wired.
import pathlib
FILES = [
    "src/eval/batch_eval.py", "src/eval/cross_model_experiment.py",
    "src/eval/simulated_eval.py", "src/eval/multi_agent_batch_eval.py",
    "src/pipelines/submit.py", "src/pipelines/submit_optimize.py",
]
def test_label_call_sites_reference_resource_labels():
    for f in FILES:
        assert "RESOURCE_LABELS" in pathlib.Path(f).read_text(), f
```
**Step 2:** Run → FAIL.
**Step 3 — implement:** at each site import `RESOURCE_LABELS` and pass `labels=dict(RESOURCE_LABELS)` to the `create_evaluation_run(...)` / `PipelineJob(...)` call (merge if a `labels=` already exists).
**Step 4:** Run → PASS. Also `uv run pytest -q` to confirm no import breakage.
**Step 5:** Commit `feat: label eval runs + pipeline jobs with solution=geap-tour`.

---

## Task 7 — Labels in shell / gcloud creation sites

**Files:** Modify `src/deploy/deploy_mcp_servers.py:20-28`; `scripts/deploy_all.sh:64,74`; `scripts/setup_logging_sink.sh:22`; `scripts/setup_model_armor.sh:20-38,48-62`; `scripts/setup_governance_policies.sh:378`. No unit tests for shell (per repo convention); guard the Python one.

**Step 1 — failing test** (Python site only):
```python
# tests/test_deploy_mcp_labels.py
from src.deploy.deploy_mcp_servers import _build_deploy_cmd  # extract cmd builder
import src.config as cfg
def test_mcp_cmd_has_labels():
    cmd = _build_deploy_cmd({"name": "search-mcp", "path": "p", "port": 8001})
    assert "--labels" in cmd and cfg.resource_labels_gcloud() in cmd
```
**Step 2:** Run → FAIL.
**Step 3 — implement:**
- `deploy_mcp_servers.py`: extract the `cmd = [...]` into `_build_deploy_cmd(server)`, append `"--labels", resource_labels_gcloud()`; import from `src.config`.
- `scripts/deploy_all.sh`: bucket create (`:64`) `--labels=solution=geap-tour`; Cloud Run deploy helper (`:74`) `--labels solution=geap-tour`.
- `scripts/setup_logging_sink.sh`: `bq mk` (`:22`) add `--label solution:geap-tour`.
- `scripts/setup_model_armor.sh`: add `"labels": {"solution": "geap-tour"}` as a sibling to `filterConfig` in both JSON bodies (best-effort; create uses `&& … || echo "may already exist"`, so if the API 400s, note it and drop the field — the retro PATCH still covers it).
- `scripts/setup_governance_policies.sh`: DNS managed-zone create (`:378`) add `--labels=solution=geap-tour`.
**Step 4:** Run the Python test → PASS. `bash -n` each edited script to syntax-check.
**Step 5:** Commit `feat: label Cloud Run, GCS, BQ, DNS, and Model Armor creates`.

---

## Task 8 — Idempotent retro-label script for existing assets

**Files:** Create `scripts/apply_resource_labels.py`; Test `tests/test_apply_resource_labels.py`.

**What:** A re-runnable script that PATCHes `RESOURCE_LABELS` onto the assets already in `hybrid-vertex`, reading ids from `.env`/config — no engine re-upload. Each section is try-wrapped (one failure doesn't abort the rest) and prints what it patched:
- **7 agent engines** (ids from `.env`): authenticated REST `PATCH https://{region}-aiplatform.googleapis.com/v1/{name}?updateMask=labels` body `{"labels": {...}}` (`google.auth.default()` + `google.auth.transport.requests`). GET first to merge with any existing labels.
- **BigQuery dataset** `geap_workshop_logs`: `google.cloud.bigquery` — `ds = client.get_dataset(...); ds.labels = {**ds.labels, **RESOURCE_LABELS}; client.update_dataset(ds, ["labels"])`.
- **4 alert policies**: `monitoring_v3.AlertPolicyServiceClient` — list by `"GEAP Workshop"` display-name prefix (like `list_quality_alerts`), set `user_labels`, `update_alert_policy(alert_policy=p, update_mask={"paths": ["user_labels"]})`.
- **Dashboard**: `monitoring_dashboard_v1` — find by display name (reuse `dashboard._find_existing`), set `labels`, `update_dashboard`.
- **Model Armor templates** (2): REST `PATCH .../templates/{id}?updateMask=labels`.
- **Cloud Run MCP services** (search/booking/expense): `gcloud run services update <name> --region … --update-labels solution=geap-tour` via `subprocess` (skip cleanly if a service is absent).
- **GCS staging bucket**: `gcloud storage buckets update gs://{GCP_STAGING_BUCKET} --update-labels=solution=geap-tour`.

**Step 1 — failing test:** unit-test the pure REST body/URL builders (no network):
```python
def test_engine_patch_request_shape():
    from scripts.apply_resource_labels import _engine_patch  # returns (url, body)
    url, body = _engine_patch("projects/p/locations/us-central1/reasoningEngines/999",
                              {"solution": "geap-tour"})
    assert url.endswith("reasoningEngines/999?updateMask=labels")
    assert body == {"labels": {"solution": "geap-tour"}}
```
**Step 2:** Run → FAIL.
**Step 3 — implement** the script: small pure helpers (`_engine_patch`, `_armor_patch`) + client-driven section functions + a `main()` that runs all sections and prints a summary.
**Step 4:** `uv run pytest tests/test_apply_resource_labels.py -q` → PASS.
**Step 5:** Commit `feat: add idempotent retro-label script for existing hybrid-vertex assets`.
**Step 6 — RUN IT LIVE** (the "update recently created assets" deliverable):
```bash
uv run python -m scripts.apply_resource_labels
```
Expect each section to print `patched: <resource>`. Note any preview/permission failures (non-blocking).

---

## Task 9 — Repoint `.env` + fail-fast coordinator smoke

The end-to-end test that proves the drift fix and surfaces any code error before touching the other engines.

**Step 1 — repoint `.env`:** set `AGENT_ENGINE_ID=4181778621234413568` (the coordinator's own id). One-time manual correction of the historical drift; future deploys keep it correct via Task 2.

**Step 2 — full offline gate:** `uv run pytest -q` → all green (existing suite + new tests). Fix any breakage before deploying.

**Step 3 — redeploy the coordinator** (applies committed runtime-builder fix + labels; `--update` reuses the existing engine, no `.env` churn):
```bash
uv run python -m src.deploy.deploy_agents coordinator --update
```
Watch for `Updated: …/reasoningEngines/4181778621234413568`.

**Step 4 — smoke a live query** (the exact path that failed with "Failed to create session"):
```bash
uv run python -m src.traffic.generate_traffic 4181778621234413568 --count 1
```
Expect a completed response, **no** session-creation error. If it errors, capture the traceback and resolve before proceeding (fail-fast).

**Step 5 — verify memory + label:**
```bash
uv run python -m src.eval.verify_memory --user-id alice --engine-id 4181778621234413568
uv run python -c "import vertexai; c=vertexai.Client(project='hybrid-vertex', location='us-central1'); e=c.agent_engines.get(name='projects/934903580331/locations/us-central1/reasoningEngines/4181778621234413568'); print(getattr(e.api_resource,'labels',None))"
```
Expect the memory read to succeed and the engine to report `{'solution': 'geap-tour'}`.

**Step 6 — commit** the `.env` repoint (it's tracked — `deploy_agents` writes it):
```bash
git add .env && git commit -m "chore: repoint AGENT_ENGINE_ID to the coordinator engine"
```

---

## Verification

**Offline (PR gate, no GCP):** `uv run pytest -q` green after every task. New tests: `test_config_labels`, `_build_config` labels + coordinator `.env` write in `test_deploy_agents`, alert/dashboard labels, metric-dimension label, `test_labels_wired`, `test_deploy_mcp_labels`, `test_apply_resource_labels`. Run `bash -n` on every edited shell script.

**On GCP (hybrid-vertex), staged:**
1. Retro-label script (Task 8 Step 6) prints a `patched:` line per asset; re-running is a no-op (idempotent).
2. Coordinator smoke (Task 9) returns a live response with no session error — the drift bug is dead.
3. `verify_memory` reads back alice's facts; `agent_engines.get(...).labels == {"solution":"geap-tour"}`.
4. Spot-check in console: BQ dataset, an alert policy, and the dashboard show the `solution=geap-tour` label.

**Success =** `.env` `AGENT_ENGINE_ID` points at the coordinator and self-heals on deploy; the runtime-scoped session/memory fix is committed and proven by a clean coordinator query; every SDK-confirmed resource (plus Model Armor best-effort + DNS) is created with `solution=geap-tour`; the 14 existing assets are patched; and the full offline suite is green.

## Risks & decisions

- **Model Armor label = best-effort.** If adding `labels` to the template create body 400s, the script's `|| echo` masks it and the template isn't created — watch Task 7's run output; drop the field if it fails and rely on the retro PATCH instead.
- **Preview surfaces intentionally skipped** (online evaluators, Agent Registry, Agent Gateways, SGP/authz) to avoid breaking preview creates with an unsupported field — documented, revisit if the APIs confirm `labels`.
- **Fail-fast on one agent (coordinator).** Only after the coordinator smoke is clean are the other 6 engines relabeled — handled non-destructively by Task 8's PATCH (no re-upload).
- **No `Co-Authored-By` trailers** (CODE_STANDARDS.md:9).
