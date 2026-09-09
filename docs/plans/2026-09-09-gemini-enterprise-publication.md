# Publish the coordinator to a Gemini Enterprise app

**Status: DEFERRED (2026-09-09).** Blocked on a Gemini Enterprise license that could
not be verified from the session that wrote this. Split out of the Skill Registry plan
so that work ships independently — nothing in this document is a prerequisite for it.

**Goal:** make the deployed coordinator selectable and usable inside a Gemini
Enterprise app, closing the last hop between "deployed on the platform" and "usable in
the product".

---

## Why this is worth doing, and why the repo previously said no

The coordinator is deployed on Agent Runtime and registered in Agent Registry, but it
is not reachable from the actual end-user product.

`notebooks/demo/platform_sdk_demo.ipynb` declined this deliberately:

> Some demos publish the reasoning engine to a Gemini Enterprise app via the raw
> `provisionedReasoningEngine` REST surface. That flow is intentionally *not* adopted
> here; `register_a2a` (above) is the supported path in this repo.

That judgement was correct when it was an undocumented raw REST call. It has since
become a **documented first-party surface** with an `agents-cli publish
gemini-enterprise` equivalent, so the reason for declining has expired. **When this
work lands, correct that notebook cell** — leaving "intentionally not adopted here"
next to a shipped publisher is exactly the kind of doc that describes behaviour the
code no longer has.

## What was verified on 2026-09-09

| Claim | Evidence |
| --- | --- |
| Registration REST shape | `POST https://{LOC}-discoveryengine.googleapis.com/v1alpha/projects/{P}/locations/global/collections/default_collection/engines/{APP_ID}/assistants/default_assistant/agents` |
| Required body | `displayName`, `description`, `adkAgentDefinition.provisionedReasoningEngine.reasoningEngine = projects/{P}/locations/{REGION}/reasoningEngines/{ID}` |
| `discoveryengine.googleapis.com` is enabled on `hybrid-vertex` | live `gcloud services list` |
| Calls need `-H "x-goog-user-project: hybrid-vertex"` | live — without it ADC attributed the call to project `681255809395` and the call failed. **This is the single non-obvious detail in the whole surface.** |
| Location compatibility | a `global` app accepts any supported region, so our `us-central1` engines work either way |
| Documented prerequisites | Gemini Enterprise Admin role; an existing GE app; agents must stay hosted on Agent Runtime |
| Five GE apps already exist in this project | `Agentspace-1757428256555`, `chief-staff-v1`, `ge-macy-app-v1`, `new-w-jw-ge`, `trends-2-creatives` — all `SOLUTION_TYPE_SEARCH`, all belonging to other teams |

**Decision already taken (owner, 2026-09-09):** create a **dedicated** app for this
demo rather than publishing into any of the five. They belong to other people in this
shared project, and an agent appearing in someone else's app is user-visible to them —
the same shared-project caution already encoded in `verify_engine_config.default_targets()`
and `find_orphan_engines`.

---

## Task 0 — Preflight (read-only, GO/NO-GO)

**Nothing is created until this passes.** Confirm and report:

* the caller holds **Gemini Enterprise Admin**;
* a **GE license** is available to assign — the blocker most likely to stop this, and
  the one that deferred the plan;
* whether creating a GE app is possible via API here or needs console provisioning.

If a license is unavailable, **stop and report. Build no partial resources.** The rest
is unusable without one, and a half-created app in a shared project is someone else's
confusing leftover.

## Task 1 — Create the dedicated app

One app named for this demo (e.g. `geap-tour`), carrying the same `solution=geap-tour`
resource labelling the rest of the repo uses, so orphan-cleanup tooling can tell ours
from the other four teams'.

Record the id in `.env` as **`GEMINI_ENTERPRISE_APP_ID`**; add
`GEMINI_ENTERPRISE_LOCATION` (default `global`). **Do not touch `AGENT_ENGINE_ID`.**

## Task 2 — Publish CLI

**Create:** `src/deploy/publish_gemini_enterprise.py`
**Modify:** `src/config.py` · **Test:** `tests/test_publish_gemini_enterprise.py`

Mirror `src/deploy/register_a2a.py`: `--publish` (default), `--list`, `--unpublish`,
`--dry-run`, with `--engine-id` / `--app-id` overrides.

**REST over `agents-cli`:** the CLI is a separate tool install, while an in-repo module
is unit-testable, matches `register_a2a.py`, and shares the existing auth path.

Must-haves:

* **Send `x-goog-user-project: {GCP_PROJECT_ID}`** (see the verification table).
* **Idempotent** — list first, update rather than duplicate. Same reasoning as
  `quality_alerts.create_quality_alert`, which had to be fixed after duplicate alert
  policies accumulated.
* Reuse `src/eval/multi_agent_batch_eval._resolve_agent_resource_name` for the full
  `projects/.../reasoningEngines/{id}` path instead of re-deriving it — getting this
  wrong is a documented failure mode (memory:
  `agent-engines-get-needs-full-name-and-region-init`).
* Nothing hardcoded — `tests/test_no_hardcoded_values.py` will catch a literal project
  or engine id.
* **Failure posture differs from A2A deliberately.** This is *not* preview-optional: a
  missing app id or a 403 is a real misconfiguration and must exit non-zero with the
  reason, not log a cheerful skip.

## Task 3 — Verify and document

```bash
uv run python -m src.deploy.publish_gemini_enterprise --dry-run
uv run python -m src.deploy.publish_gemini_enterprise --publish
uv run python -m src.deploy.publish_gemini_enterprise --list    # coordinator present
```

API-level verification is automatable; **confirming the agent is selectable and answers
in the GE UI is a human step.** Say so plainly rather than implying end-to-end proof.

Then: write `docs/notes/gemini-enterprise-publication.md`, add its index line to
`docs/notes/README.md` (**at 199 of a 200-line budget — reclaim a line first**), and
correct the stale declination in `notebooks/demo/platform_sdk_demo.ipynb`.

---

## Caveats

* **Licensing is the gating risk.** Task 0 finds out for free.
* **Shared project.** Do not touch the other five apps. Label ours.
* Out of scope: publishing the router or tier agents; GE `authorizationConfig` /
  `toolAuthorizations` for user-delegated access to Google Cloud resources.
