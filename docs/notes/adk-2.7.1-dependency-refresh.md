# ADK 2.6.3 → 2.7.1 + full dependency refresh

*Recorded 2026-08-21. Supersedes the "future upgrade plan" seed in
[agentplatform-client-migration.md](./agentplatform-client-migration.md).*

`google-adk` moved to an exact `==2.7.1` pin (`pyproject.toml`,
`deploy_agents.REQUIREMENTS`) and everything else was refreshed with
`uv lock --upgrade`.

## What moved

| package | 2026-08-21 |
| --- | --- |
| `google-adk` | 2.6.3 → **2.7.1** (exact pin, both places) |
| `google-cloud-aiplatform` | 1.163.0 → 1.165.1 |
| `google-genai` | 2.17.0 → 2.19.0 |
| `litellm` | 1.85.7 → 1.96.2 |
| `ruff` / `ty` | 0.16.2 → 0.16.4 / 0.0.70 → 0.0.73 |

Plus ~18 transitive bumps (`tiktoken`, `nltk`, `uvicorn`, `pygments`,
`platformdirs`, the `charset-normalizer`/`idna`/`requests` trio, …).

**Held back by upstream constraints, not by choice:** `mcp` 1.29.0 (2.0.0 exists),
`openai` 2.54.0 (3.3.1), `protobuf` 6.33.6 (7.36.0), `websockets` 15.0.1 (17.0.1).
The resolver caps these; forcing them means overriding a pin inside `google-adk` /
`litellm` / `google-cloud-aiplatform`. Re-check after the next ADK release.

### One dependency had to be *added*

`uv lock --upgrade` **dropped `google-cloud-trace`** (it was only ever present
transitively, via a package that no longer requires it). But
`src/observability/fetch_trace.py` imports `google.cloud.trace_v1` for real. It is
import-safe — it prints an install hint instead of crashing — so this would have
degraded *silently* into a permanently broken CLI. Now declared explicitly in
`pyproject.toml`. `google-cloud-iam` and `opentelemetry-exporter-gcp-trace` were
also dropped; nothing imports either, so they stay gone.

## The one real code change: `PreloadMemoryTool` drift

`src/agents/caching_preload_memory_tool.py` subclasses ADK's `PreloadMemoryTool`
to memoize the retrieve per `(invocation_id, query)`. ADK **inlines** its render
into `process_llm_request` with no hook to delegate to, so our subclass carries a
verbatim copy — and in 2.7.0 upstream changed it:

```python
# ADK <= 2.6.x — memories land in the system-instruction channel
llm_request._append_dynamic_instructions([si])

# ADK >= 2.7.0 — memories land as a transient *user* turn at the current-turn boundary
llm_request._insert_transient_user_content(
    [types.Content(role="user", parts=[types.Part.from_text(text=memory_context)])]
)
```

**Both methods still exist on `LlmRequest` in 2.7.1** (`llm_request.py:116` and
`:302`), so the stale copy kept "working" — no exception, no warning — while
putting recalled memories in a different part of the prompt than every stock ADK
agent. That is the failure mode this upgrade was most at risk of.

Why the test suite did not catch it: `tests/test_caching_preload_memory_tool.py`
used a `_FakeLlmRequest` that implemented `_append_dynamic_instructions` itself.
**A fake that duck-types the very API it is meant to be checking can never detect
upstream drift.** Replaced with a real `LlmRequest` plus a *differential* guard,
`test_render_matches_stock_adk`, which runs stock `PreloadMemoryTool` and
`CachingPreloadMemoryTool` over identical requests and asserts the whole
`model_dump()` (plus the private `_dynamic_instructions`) matches. It fails on the
pre-fix code and pins any future upstream move. Keep it passing on the next bump.

## `GOOGLE_GENAI_USE_VERTEXAI` is deprecated

New in this upgrade: ADK's `env_utils.is_enterprise_mode_enabled()` reads
`GOOGLE_GENAI_USE_ENTERPRISE` first and only falls back to
`GOOGLE_GENAI_USE_VERTEXAI` **with a `DeprecationWarning`**. google-genai 2.19.0
does the same (`_api_client.py:653`) and warns separately if the two *conflict*.

Both spellings are now set, with identical values, in the three places we control:
`deploy_agents._build_config` (deployed engine env), `optimize_pipeline._RUNTIME_ENV`,
and `tests/conftest.py`. Same value ⇒ no conflict warning; the old name stays for
anything on the managed runtime that only knows it. Semantics are unchanged —
`is_enterprise_mode_enabled()` returned `True` under either spelling.

`.env` still sets only the old name, so **local** CLI runs still print the
deprecation. Adding `GOOGLE_GENAI_USE_ENTERPRISE=1` there silences it; left alone
deliberately (`.env` is not edited by this repo's tooling).

## What did NOT break — audited, not assumed

Every ADK private/internal API this repo reaches into still exists in 2.7.1
(11/11, checked by importing each against the installed wheel):

`LlmRequest._append_dynamic_instructions`, `LlmRequest._insert_transient_user_content`,
`tools/_memory_entry_utils.extract_text`,
`LocalEvalService._evaluate_single_inference_result`,
`LocalEvalSampler._extract_eval_data`,
`optimization.gepa_root_agent_prompt_optimizer`,
`plugins.bigquery_agent_analytics_plugin`, `AgentRegistry.get_mcp_toolset`,
`PreloadMemoryTool.process_llm_request`.

**Both router workarounds are still necessary** — do not delete them:

- `flows/llm_flows/contents.py` still gates the `adk-` tool-call-id strip on
  `isinstance(canonical_model, LiteLlm)`, so `RetryingLlm`/`TierRoutingLlm` still
  hide `LiteLlm` from it and `restore_tool_call_ids()` is still load-bearing
  ([router-empty-stream-retry.md](./router-empty-stream-retry.md)).
- `models/lite_llm.py` still has `effective_model = llm_request.model or self.model`,
  so the Claude-tier `vertex_ai/` prefix loss is unfixed upstream.

## Gotcha: the suite silently collects 39 fewer tests without `--all-groups`

`tests/conftest.py:23` `collect_ignore`s the DOE and pipeline test modules when
`pyDOE3` / `kfp` are missing. A bare `uv run <anything>` re-syncs the venv to the
**default** groups, which evicts `pyDOE3` — and the next `uv run pytest` reports
`1093 passed` instead of `1132`, with no skips, no errors, and no hint that 39
tests vanished. Pre-existing behavior, not caused by the upgrade, but it bites
hardest during one: always gate on `uv sync --all-groups && uv run pytest`.

## Verification run for this upgrade

- `ruff format --check` / `ruff check` clean. `ty check src/` is **fully clean** for
  the first time, but not for the reason it first appeared: the long-standing
  `src/doe/design.py:21 unused ty: ignore` diagnostic is **group-dependent**, not a
  fixed baseline. `pyDOE3` / `kfp` live in the optional `doe` / `pipelines` groups,
  so those `# ty: ignore[unresolved-import]` comments are load-bearing on a default
  `uv sync` and dead under `uv sync --all-groups` — the same command exits 0 or 1
  depending on which groups the caller synced (6 diagnostics under `--all-groups`).
  Fixed properly by turning the `unused-ignore-comment` rule off in
  `[tool.ty.rules]` rather than by deleting ignores that another sync still needs.
- `uv sync --all-groups && uv run pytest` → **1132 passed**. Test *collection* is
  byte-identical pre- and post-upgrade (verified by diffing per-file collect counts
  against the 2.6.3 venv), so nothing was silently gained or lost.
- Live, both engines updated **in place** (never recreated) on 2.7.1: router
  `6134089059699523584` and coordinator probe `4380288848559603712`.
- **`verify_cross_session_recall --user-id alice` → `RECALL: PASS`.** This is the
  end-to-end proof of the preload fix: a preference stated in session A resurfaces
  in a brand-new session B, so the memory block still reaches the model through the
  new transient-user-content channel. Run it after any ADK bump.
- **Router health**: first probe right after the redeploy read 2/28 silent empties
  (7.1%, FAIL). A larger re-run on the warmed engine read **56/56 FULL, 0.0%**
  (PASS) — cold-start noise on a minutes-old engine, not a 2.7.1 regression. Worth
  remembering: `verify_router_health` immediately after a deploy is measuring
  warm-up, not the build.

### One rubric metric moved — `hallucination`

> **RESOLVED 2026-08-21 — it was not judge drift.** The `hallucination` movement
> below is explained by two eval-harness defects, chiefly that `agent_data.agents`
> was `None` so the judge was told the agent had no tools and graded real tool calls
> as contradictory. Fixed; `hallucination_v1` now reads 0.90-0.93. See
> [offline-eval-empty-turns.md](./offline-eval-empty-turns.md). Do not pursue the
> aiplatform 1.163 → 1.165 judge-template hypothesis suggested below.

Five 49-case coordinator batch evals on the same probe engine, three before the
upgrade and (so far) three after:

| run (UTC) | match | quality | **halluc** | instr | safety | tooluse |
| --- | --- | --- | --- | --- | --- | --- |
| 08-20 16:23 | 0.71 | 0.81 | 0.69 | 0.63 | 1.00 | 0.42 |
| 08-20 17:15 | 0.66 | 0.77 | 0.75 | 0.70 | 0.97 | 0.38 |
| 08-20 17:28 | 0.68 | 0.82 | 0.68 | 0.63 | 0.98 | 0.38 |
| 08-21 02:30 | 0.74 | 0.76 | 0.81 | 0.68 | 0.96 | 0.38 |
| **mean pre-2.7.1 (n=4)** | 0.70 | 0.79 | **0.73** | 0.66 | 0.98 | 0.39 |
| 08-21 03:44 | 0.76 | 0.79 | 0.60 | 0.67 | 0.97 | 0.36 |
| 08-21 03:59 | 0.65 | 0.77 | 0.67 | 0.64 | 0.96 | 0.40 |
| 08-21 04:15 | 0.74 | 0.62 | 0.62 | 0.58 | 0.94 | 0.39 |
| **mean post-2.7.1 (n=3)** | 0.72 | 0.73 | **0.63** | 0.63 | 0.96 | 0.38 |

`hallucination` dropped 0.73 → 0.63 and **all three** post-upgrade runs fall below
the pre-upgrade minimum of 0.68. Three draws being the three lowest of seven is
`1/C(7,3)` ≈ **p 0.03** under a no-change null, so this is probably a real shift
rather than the metric's usual 0.68-0.81 wobble. The other five metrics are flat
or within their existing spread. (`tool_use_quality` at ~0.38 is the long-standing
delegation-blind SDK metric that `geap_tool_use` replaces in the publish path —
unrelated and unchanged.)

**It is NOT the preload change.** The obvious suspect was the new memory placement
— recalled memories now arrive as a *user turn* instead of a system instruction,
which a grounding judge could plausibly read differently. Checked, and ruled out:
`multi_agent_batch_eval.py:83` runs every case as `user_id="eval-batch-user"`, and
`verify_memory --user-id eval-batch-user` returns **no persisted memories**. With
an empty `response.memories` the render returns early, so the changed line never
executes in this eval. Whatever moved, it is not this.

The leading remaining candidate is **judge-side drift, not an agent regression**:
`google-cloud-aiplatform` went 1.163.0 → 1.165.1 in the same change, and that is
the package that ships the Gen AI Evaluation Service client and its rubric
templates. A scoring-side change would move the number with identical agent
behavior. Not confirmed — separating them means re-scoring a fixed set of captured
responses across the two SDK versions, which is a follow-up, not part of this
upgrade. Until then: treat the `hallucination` series as having a **level shift at
2026-08-21**, and do not compare across that boundary.
