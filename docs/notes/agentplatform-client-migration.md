# `vertexai.Client` → `agentplatform.Client`

*Recorded 2026-08-21.*

The trigger, seen in local CLI output:

```
FutureWarning: The vertexai.Client class is deprecated. Please use agentplatform.Client instead.
  return vertexai.Client(project=GCP_PROJECT_ID, location=GCP_REGION)
```

That echoed line is ours — `GCP_PROJECT_ID` / `GCP_REGION` are this repo's config
names and match `src/eval/verify_memory.py` and `src/eval/seed_demo_memories.py`
verbatim. Every `Client` this repo constructs now comes from `agentplatform`, and
the evals-SDK monkeypatches moved with it.

## What the SDK actually does

- The warning is emitted from `vertexai/_genai/client.py:245-251` behind a
  module-global `_CLIENT_WARNING_SHOWN` — **once per process**, not per call. This
  is log hygiene and future-proofing, not a volume problem.
- **No dependency moved.** `agentplatform` ships inside the same
  `google-cloud-aiplatform==1.163.0` distribution already pinned by
  `pyproject.toml` and `deploy_agents.REQUIREMENTS` (97 `agentplatform` files in the
  dist RECORD). Nothing was added to `uv.lock`.
- `agentplatform/__init__.py` mirrors `vertexai/__init__.py`: same
  `__all__ = ["init", "preview", "Client", "types"]`, same PEP 562 lazy
  `__getattr__`. Consequence: `import agentplatform.types` raises
  `ModuleNotFoundError` — use `from agentplatform import types`.
- `agentplatform.Client` is a **superset**: every property we use (`evals`,
  `agent_engines`, `prompt_optimizer`) plus `rag`, `model_garden`,
  `feedback_entries`; every `Evals` method this repo calls exists on both.
- **Only `Client` is deprecated.** `vertexai.init` emits no warning and
  `agentplatform.init` *is* `vertexai.init` (both re-export
  `google.cloud.aiplatform.init` — the identical function object).
  `vertexai.agent_engines` is not deprecated either.

## Why this was not a mechanical rename

`agentplatform._genai` is a **separate copy**, not an alias:

- `agentplatform.types is vertexai.types` → **False**. Distinct module objects with
  distinct pydantic classes (538 `extra="forbid"` models in `vertexai.types`, 718 in
  `agentplatform.types`).
- `agentplatform/_genai/_evals_common.py` differs from `vertexai`'s by **1359 diff
  lines** — a newer implementation, not a rename.

`src/eval/_sdk_patches.py` monkey-patches three symbols in that module
(`AGENT_MAX_WORKERS`, `_process_single_turn_agent_response`,
`_execute_agent_run_with_retry`) plus `types`. **All four bugs still exist in the
`agentplatform` copy with identical signatures** — verified 2026-08-21 that
`_process_single_turn_agent_response` still does
`resp_item[-1]["content"]["parts"][0]["text"]` — so all four patches are still
required, and all four had to be retargeted.

That is the whole hazard of this change: patching `vertexai._genai` while
constructing an `agentplatform.Client` is a **silent no-op**. It reinstates the
"Failed to parse agent run response" stub that collapses every rubric metric to
~0. Same coupling for `types`: a `vertexai.types.evals.AgentInfo` handed to an
`agentplatform` client is a foreign pydantic class, and `_flip_extra_to_ignore`
would flip the wrong package's models.

**Client, `types`, and the `_sdk_patches` targets are one atomic unit. Partial
migration is worse than none.** `tests/test_agentplatform_client.py` pins all three:
a repo-wide grep guard over `src/`, an assertion that
`multi_agent_batch_eval.Client` and `.types` both resolve to `agentplatform`, and a
`warnings.catch_warnings` check that constructing the client is FutureWarning-free.

## This does NOT quiet the deployed engines

A census of the engine logs found **every occurrence comes from ADK, not from us** —
re-run *after* this migration landed and still 200/200:

```
$ gcloud logging read 'textPayload:"The vertexai.Client class is deprecated"' --freshness=1d
  164  .../google/adk/sessions/vertex_ai_session_service.py:527   # ADK 2.6.3 (our exact pin)
   36  .../google/adk/sessions/vertex_ai_session_service.py:534   # ADK 2.7.x, engine 5638288480409747456
# by engine: 212 router 6134089059699523584 + 88 probe 4380288848559603712 (both :527)
```

The line number tells you the ADK version: 2.6.3 builds `vertexai.Client` at
`vertex_ai_session_service.py:523,527`, 2.7.1 at `:530,534`. Both our live engines
are on the 2.6.3 exact pin (`deploy_agents.REQUIREMENTS`, `pyproject.toml`).

ADK constructs `vertexai.Client` in `sessions/vertex_ai_session_service.py` and
`memory/vertex_ai_memory_bank_service.py` — both instantiated by
`deploy_agents._session_service_builder` / `memory_service_builder` on every
deployed engine. **Do not claim a log-volume reduction on the engines**, and do not
propose an ADK bump as the remedy (see below). What this change cleans up is *local*
CLI output — the deploy CLI and the eval/verify CLIs, which is where the warning was
seen.

## Deliberately out of scope

- **`vertexai.init` (23 sites).** Not deprecated; `agentplatform.init` is literally
  the same function object. Pure churn.
- **`from vertexai import agent_engines` (11 sites) and `AdkApp`.** Not deprecated,
  and `_build_app()`'s `AdkApp` instance is **cloudpickled into the served engine**.
  `agentplatform.agent_engines` is a separate copy of the templates; swapping the
  class that runs `set_up()` on the managed runtime is exactly the class of change
  that produced the empty-stream saga, for zero warning benefit. Mixing is safe:
  `agentplatform`'s `_validate_agent_or_raise` checks `Queryable`/`StreamQueryable`,
  which are **structural Protocols**, so a `vertexai` `AdkApp` passes validation
  against an `agentplatform` client (exercised live by `deploy_agents --update`).
- **Mixed imports are intentional, not an oversight.** `src/deploy/deploy_agents.py`
  and `src/eval/seed_demo_memories.py` import from *both* packages —
  `agentplatform` for `Client`, `vertexai` for `init` / `agent_engines`. Both carry
  a comment pointing here.
- **`notebooks/jt_eval_jw.ipynb`** — untouched by request. The other two notebooks
  (`multi_agent_eval_sim.ipynb`, `intro_to_skill_registry.ipynb`) had their import
  cells swapped, no re-execution.

## The ADK upgrade is a separate plan (2.6.3 → 2.7.1)

Checked 2026-08-21 and **an ADK bump does not fix the warning.** The 2.7.1 wheel
(latest, released 2026-08-17) still calls `vertexai.Client` at
`sessions/vertex_ai_session_service.py:530,534`, and likewise in
`memory/vertex_ai_memory_bank_service.py:630`,
`evaluation/vertex_ai_eval_facade.py`,
`evaluation/_vertex_ai_scenario_generation_facade.py`, `cli_deploy.py:1191,1198`,
and `code_executors/agent_engine_sandbox_code_executor.py`. Only the new
`memory/vertex_ai_rag_memory_service.py` — which this repo does not use — has moved
to `agentplatform`. The engine-log noise is upstream's to clear, on no known
timeline.

The upgrade is still worth doing on its own merits. Seed findings for that plan:

- **301 changed files**, including `lite_llm.py` (978 diff lines), `contents.py`
  (475), `mcp_toolset.py` (461).
- **One silent hazard:** `preload_memory_tool.py` moved from
  `_append_dynamic_instructions([str])` to
  `_insert_transient_user_content([Content(...)])`.
  `src/agents/caching_preload_memory_tool.py:153` reimplements the **old** way, and
  the old method still exists in 2.7.1 — so it would diverge silently rather than
  crash.
- Every other ADK private API this repo touches survives 2.7.1 unchanged:
  `LlmRequest._append_dynamic_instructions`, `tools/_memory_entry_utils`,
  `LocalEvalService._evaluate_single_inference_result`,
  `LocalEvalSampler._extract_eval_data`, `gepa_root_agent_prompt_optimizer`,
  `AgentRegistry.get_mcp_toolset`, the BigQuery analytics plugin.
- Both router workarounds remain necessary: `contents.py` still gates the `adk-`
  tool-call-id strip on `isinstance(canonical_model, LiteLlm)` (see
  [router-empty-stream-retry.md](./router-empty-stream-retry.md)), and
  `lite_llm.py:2939` still has `effective_model = llm_request.model or self.model`
  (the Claude-tier `vertex_ai/` prefix loss).

## Verifying it stayed migrated

```bash
uv run pytest tests/test_agentplatform_client.py tests/test_sdk_patches.py -q
uv run python -W error::FutureWarning -m src.eval.verify_memory \
  --user-id alice --engine-id 4380288848559603712
# The patches' real proof: real rubric scores, not ~0 / "Failed to parse agent run response"
uv run python -m src.eval.multi_agent_batch_eval --agents coordinator_agent \
  --agent-id 4380288848559603712 --limit 4
```
