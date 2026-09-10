# The Skill Registry: the publish half is real, the discovery half reads a different store

**Date:** 2026-09-09/10 · **Project:** `hybrid-vertex` · branch `skill-registry`.

Three travel/expense skills are defined in git, published to a live registry, and
wired into the coordinator behind an opt-in flag. Publishing works and is
idempotent against the real service. **Runtime discovery does not work in this
project, and the reason is not our code:** the API we publish to and the API ADK
reads from are two different services with two different backing stores. This
note records what was measured, what the measurement means, and what is still
assumption.

## What shipped

* **`src/skills/definitions.py`** — the three skills (`expense-policy-triage`,
  `trip-planning-brief`, `receipt-audit`) as frozen `SkillDefinition`s, plus
  `materialize_skill()`. The registry is a deployment target, not the source of
  truth: prose that steers a production agent lives in git where it is diffable.
  Instruction-only — `code_executor` is deliberately unset, so nothing here drags
  in a sandbox and an arbitrary-code-execution surface for no demo value.
* **`src/skills/publish_skills.py`** — `--publish` (default) / `--list` /
  `--search` / `--delete` / `--dry-run`, create-or-update. Three-way posture:
  registry surface absent → logged skip and exit 0; a call the registry understood
  and refused → exit non-zero; success → N-of-M.
* **`src/skills/toolset.py`** — `get_skill_toolset()` builds an ADK
  `SkillToolset(registry=GCPSkillRegistry(...))` and degrades to `None` + a WARNING
  on any failure (it runs at agent *import* time). Wired into the coordinator
  behind `ENABLE_SKILL_REGISTRY` (**default OFF**), with `SKILL_REGISTRY_LOCATION`
  as the one constant both halves share; both are baked into engine env at deploy
  time, because the container picks its tool list from its own env, not the
  operator's shell.

## What was verified live

1. **Publishing is real.** `--publish` created all three skills in
   `projects/hybrid-vertex/locations/us-central1/skills`. `--dry-run` makes no
   calls, `--list` enumerates, exit codes are correct on each path.
2. **Idempotency holds against the real service**, which until now was only
   modelled offline against a fake. The registry went **116 → 119** skills across
   **two** consecutive publish runs of three skills; the second run logged
   `updated`, not `created`. A non-idempotent publisher would have left 122.
3. **The toolset genuinely reaches the deployed agent.** With
   `ENABLE_SKILL_REGISTRY=1` on the probe engine `4380288848559603712`, a captured
   trajectory (`src/eval/tool_faithfulness.capture_interaction`) shows the
   coordinator calling `list_skills` and `search_skills`, both returning. The
   exposed tool names are exactly `list_skills`, `load_skill`,
   `load_skill_resource`, `search_skills` — `run_skill_script` is filtered out, as
   intended: with instruction-only skills a script runner can only ever error.

So the plumbing is end-to-end. What it plumbs into is the problem.

## The blocker: two services, two stores

`client.skills` (our publisher) and ADK's `GCPSkillRegistry` (the agent's reader)
are **not two ends of one system**:

* **Writer** — `agentplatform.Client().skills` (our publisher) writes to
  `us-central1-aiplatform.googleapis.com/v1beta1/projects/…/locations/us-central1/skills`.
  That store holds our 3 skills plus 116 others.
* **Reader** — ADK's `GCPSkillRegistry` reads
  `https://agentregistry.googleapis.com/v1alpha` (its `base_url`, overridable via the
  `AGENT_REGISTRY_ENDPOINT` env var). At `locations/global` that holds **100 skills,
  all Google-published** (`discoveryengine.googleapis.com-*`, `cloud.google.com-*`) —
  none of ours. At `locations/us-central1` it returns **503 UNAVAILABLE**.

**Pointing the toolset at `global` does not rescue it.** Agent Registry's
`skills:search` returns `{}` in this project even for its own 100-skill catalogue
— verified twice, through `GCPSkillRegistry.search_skills` (0 hits) and a direct
`curl` to `…/locations/global/skills:search`. ADK's discovery path therefore finds
nothing regardless of what we publish, or where.

**Agent Registry does accept custom creates**, but under a different model. A
`POST …/locations/global/skills?skillId=…` fails `400 "type is required"` — not
404, not 501, so the method is served. Its entries carry `type: "SIMPLE"`, a
`frontmatter` block, a `publisher` resource (`…/publishers/cloud.google.com`) and
a URN-shaped id (`urn:skill:cloud.google.com:run:cloud-run-basics`). Publishing a
second copy of the skills into that shape was considered and **deliberately not
done**: with `skills:search` answering `{}`, discovery would almost certainly still
fail, and the run would have proved the same blocker twice at the cost of three
more entries in a shared registry.

### Read the empty `list_skills` correctly

On the deployed agent, `list_skills` returned
`{"result": "<available_skills>\n</available_skills>"}` — empty, not an error. It
is tempting to read that as "the registry is empty". It is not evidence of
anything about the registry:

* ADK's `ListSkillsTool.run_async` renders `SkillToolset._list_skills()`, which
  returns `self._skills` — populated **only** from the constructor's `skills=`
  argument (`skill_toolset.py`, `self._skills = {skill.name: skill for skill in
  skills}`). We pass `registry=` and no `skills=`, so `list_skills` renders empty
  **whatever the registry holds**, healthy or not.
* The registry-backed paths are `search_skills` (→ `skills:search`) and
  `load_skill` (→ `skills.get` plus a media download of `defaultRevision`).

That distinction also relocates the *silent* failure. A 503 is loud: ADK's
`_make_request` raises, and `SearchSkillsTool` returns
`{"error": …, "error_code": "REGISTRY_ERROR"}` to the model. The quiet one is the
`{}` at `global`: `GCPSkillRegistry.search_skills` does
`response_data.get("skills", [])`, so an HTTP 200 with no `skills` key becomes an
empty result list with no error at any layer — a "discovery is not served here"
that is byte-identical to "no skill matches your query". That is
[a check that cannot detect its own failure](./checks-that-cannot-detect-their-own-failure.md),
inside the SDK rather than in our code: nothing short of reading the wire
distinguishes the two, which is why the blocker had to be chased to the endpoint
rather than argued about from the agent's behaviour.

(This corrects the first reading of the run, which took the empty `list_skills`
as the swallowed `503` surfacing. The `503` and the two-store split are both real;
the empty `list_skills` is simply not the symptom of either.)

**Follow-up this implies for `SKILL_TOOL_NAMES`.** `run_skill_script` is filtered
out because with no `code_executor` it can only ever error. By the same argument
`list_skills` is a candidate: constructed registry-only, it can only ever render
an empty list, so it spends a tool slot to tell the model nothing. Left in for now
— that is a tool-surface change, and the flag is off anyway — but it should be
decided deliberately rather than inherited, and measured against
`tool_use_accuracy` alongside the flag itself.

## Semantic retrieval on the store that *does* hold our skills lags by hours

`--search` never returned our three skills across ~40 minutes of retries, with
queries worded directly at their descriptions. This is **index lag, not
description quality**, on three pieces of evidence:

* skills created 2026-09-08 19:30 (`nova-probabilistic-forecaster-*`) *are*
  returned by the same `retrieve` call;
* ours, created 2026-09-09 23:17, are not;
* a direct `skills.get` on all three succeeds and shows the correct,
  retrieval-friendly descriptions — so they are stored, findable by name, and
  merely not indexed yet.

**Operational consequence: you cannot publish a skill and immediately demo
semantic discovery.** Publish ahead of time.

## Why no offline test could have caught the split

Both halves are internally consistent and both are covered by tests that pass.
`src/config.py` already guards the *in-repo* version of this bug — one
`SKILL_REGISTRY_LOCATION` constant, so the publisher cannot write where the
toolset does not read — and that guard is sound and irrelevant here: the two
halves disagree about the **host**, not the location, and the disagreement lives
between two Google products, not between two of our modules. Against a fake
client, a publish that lands nowhere the agent looks is byte-identical to one that
lands correctly. The only signal is a live agent that discovers nothing, which is
also what a live agent looks like when a query legitimately matches no skill.

## What state this left behind

* **`ENABLE_SKILL_REGISTRY` is default OFF everywhere and no served engine carries
  it.** The probe engine briefly ran with it on and has been **reverted to
  flag-off**: leaving it would have given the model four tools that return
  nothing, and the tool surface is an input to `tool_use_accuracy`, one of the
  three rubric metrics behind the monitored `agent_eval/*` series.
* **The three published skills were left in place** in the aiplatform store. They
  are ours and they are the evidence the publish path works; `--delete <id>`
  removes them.
* **The registry is shared, and already shows the failure mode idempotency exists
  to prevent.** Of the 116 pre-existing skills, only **42 display names are
  distinct** — 74 are duplicates: `probabilistic-forecaster` ×16, and
  `Sample math skill` ×4 from this repo's own
  `notebooks/intro_to_skill_registry.ipynb`, which mints `math-skill-{timestamp}`
  on every run. The duplicate accumulation that motivated create-or-update is not
  hypothetical; some of it is our prior art.

## A deploy-hygiene practice worth naming

Flipping the flag on the probe meant an in-place `deploy_agents … --update`, and
an update **rebuilds engine env from the current process**. A naive one would have
silently:

1. moved the backbone `gemini-2.5-flash` → `gemini-3.5-flash`, which breaks
   server-side Model Armor (templates `400 TEMPLATE_NOT_FOUND` on the global
   endpoint) — precisely why that engine is pinned to 2.5;
2. dropped `ENABLE_MEMORY_PRELOAD_CACHE`;
3. dropped `ENABLE_MODEL_ARMOR_PLUGIN` — its default flips in the unmerged PR
   #111, and this branch is cut from `main`.

All three were caught by **computing the prospective env with `_build_config` and
diffing it against the live engine before deploying**. That pre-flight costs one
local call and no deploy, and this exact class of silent drop has bitten this
engine before. The applied diff ended up being exactly the two intended additions,
with `16Gi` / `4` CPU / `min_instances=4` preserved.

## Writing a skill body the tests will accept

`tests/test_skill_definitions.py` grades the **backticked identifiers** in a skill
body against the real MCP surface, and the rules are enforced nowhere a skill
author would otherwise look:

* `` `name(` `` — a backticked call site — is graded against the real MCP tool
  names, read out of `src/mcp_servers/*/server.py`. Invented tools fail.
* A bare backticked lowercase identifier is graded as a **response-field claim**,
  and the allowed set is scoped to the tools *that same skill* tells the agent to
  call. So a skill may only read fields it also fetches — which is what forces
  `receipt-audit` step 1 to fetch what its later steps consume.
* The field regex accepts a `field: value` form, and that is the escape hatch for
  writing a **value**: `` `status: "pending_review"` `` passes (the claim is
  `status`, a real field), while `` `pending_review` `` fails — nothing returns a
  field by that name.
* Two allowances exist, both deliberately narrow: the policy `category`
  vocabulary, derived from `POLICY_LIMITS`, and a one-entry
  `_NON_FIELD_BACKTICKED_TERMS` (`cancelled`, a *value* of `status`).
* Consequently discrepancy-class names — `date_mismatch`, `amount_mismatch`,
  `clean_match` — are written **un-backticked** (bolded instead). They are our
  vocabulary, not the tools'.
* Floors that keep the check non-vacuous: ≥ 15 distinct field claims across all
  skills collectively, > 80 words per body, ≥ 12 words per description, and the
  description must contain "use this skill when" (semantic retrieval scores the
  request against that text).

## Re-checking the blocker

```bash
# Our store: the three skills are there, by name.
uv run python -m src.skills.publish_skills --list

# Semantic retrieval on our store (expect nothing for a freshly published skill).
uv run python -m src.skills.publish_skills --search "split a receipt by category"

# The same store, raw — what the publisher writes to.
TOKEN=$(gcloud auth print-access-token)
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/hybrid-vertex/locations/us-central1/skills"

# What ADK reads instead: 100 Google-published skills at global, 503 at us-central1.
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://agentregistry.googleapis.com/v1alpha/projects/hybrid-vertex/locations/global/skills"
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://agentregistry.googleapis.com/v1alpha/projects/hybrid-vertex/locations/us-central1/skills"

# The discovery call itself — `{}` even for Agent Registry's own catalogue.
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://agentregistry.googleapis.com/v1alpha/projects/hybrid-vertex/locations/global/skills:search?search_string=cloud+run"

# Same question through ADK's client, which is what the agent actually uses.
uv run python -c "
import asyncio
from google.adk.integrations.skill_registry import GCPSkillRegistry
r = GCPSkillRegistry(project_id='hybrid-vertex', location='global')
print(asyncio.run(r.search_skills(query='expense policy triage')))
"
```

## Still unverified

* **Whether the two stores are ever joined.** Single project, two preview
  surfaces. That `skills:search` returns `{}` for Google's own catalogue suggests
  the discovery path is simply not serving here, but nothing rules out a project
  or an enrolment where publishing to `client.skills` *is* what ADK reads.
* **Whether publishing into Agent Registry's own model works.** Not attempted. The
  `400 "type is required"` proves the method exists; that discovery would still
  fail is **inference from the `{}`, not a measurement**.
* **The real length of the index lag.** Bounded below at ~40 minutes; a skill
  ~28 hours old does come back. The upper bound was never measured.
* **The tool-surface cost of the flag.** Four extra tools were never scored
  against `tool_use_accuracy`. That measurement is the precondition for flipping
  the default, and it has not been made.
* **`load_skill` end-to-end.** Never exercised on a skill of ours, because
  discovery never surfaced one to load.

Related: [checks that cannot detect their own failure](./checks-that-cannot-detect-their-own-failure.md),
[Agent Registry MCP resolution](./agent-registry-mcp-resolution.md) (the same
`agentregistry.googleapis.com` control plane, a different failure surface),
[the deployed-engine baseline](./deployed-engine-baseline.md).
