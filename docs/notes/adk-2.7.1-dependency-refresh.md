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

## Live validation of the ADK 2.8.0 / aiplatform 2.x change set (2026-09-09)

A green suite had already proved nothing twice on this change set, so everything below
was run against live infrastructure. **The suite passed at 1648 while no deploy of any
kind worked.**

### Three bugs the tests could not reach

All three were found by deploying and driving a real engine.

| Bug | Why no test caught it |
| --- | --- |
| `AttributeError: 'Client' object has no attribute 'agent_engines'` in `create_agent`/`update_agent` | Every deploy test injects a **fake client**, so the fakes kept answering an attribute aiplatform 2.x had deleted. The tests asserted against a mock of an API that no longer exists. |
| `AttributeError: 'RetryingLlm' object has no attribute 'startswith'` in armor plugin selection | `agent.model` is a `BaseLlm` wrapper on every real agent; the deploy tests build **fake agents** whose `.model` is a plain string. |
| `TypeError: get() got an unexpected keyword argument 'name'` at six `vertexai.agent_engines.get` sites | Five sit inside best-effort `try/except` warm-up blocks, so the only symptom was `Warmup skipped: …` — the cold-start protection that exists to prevent empty-at-200 was **silently gone**. |

The common shape: *tests that mock the thing that changed cannot see it change.* The
fixes are guarded by tests that use the **real** surfaces — a live `agentplatform`
Client, the real coordinator/router agents, and the actual `agent_engines.get` signature.

### A fourth, pre-existing: recall had no SSE fallback

`verify_cross_session_recall` was the one live-streaming module still calling
`stream_query` directly. Nine others absorb the NDJSON parser skew via `raw_stream.py`.
A healthy probe engine therefore reported `DEMO READINESS: NOT READY` on a check marked
critical — while `engine_live` **passed on the same engine in the same run**. That
contrast is the tell for a parser skew rather than a broken agent.

### A deploy trap worth knowing

An in-place `--update` **silently drops any opt-in flag not set in the deploying
shell's environment**. The first probe update lost `ENABLE_MEMORY_PRELOAD_CACHE=1`
because the deploying shell did not export it. Same family as the router tier-override
trap. Capture `verify_engine_config --json` *before* an update and diff after —
the second deploy restored it, and the final diff against pre-state was **zero
changed findings**: only the container moved.

### What was verified live, and what it returned

| Surface | Result |
| --- | --- |
| Probe engine deploy (ADK 2.8.0 + aiplatform 2.1.0 + `google-cloud-modelarmor`) | container builds; `config: PASS`, 0 critical |
| `demo_readiness --deep` | **READY** — all 6, incl. genuine cross-session recall |
| `trajectory_eval` (**preview `EvalTask`** — the highest-risk import) | works: 39 cases, exact_match 0.74 / precision 0.87 / recall 0.97 |
| `tool_faithfulness` | 5.00/5 over 3/3, no hallucinated actions |
| `online_monitor --dry-run` | 4/4 scored, 0% infra-empty, helpfulness 4.0 |
| `calibration --panel` (real judges, concurrent) | PASS, alpha 0.992-0.996, **0 unparseable** |
| Serving requirement set | resolves fastmcp 3.4.7 / mcp 1.30.0 / aiplatform 2.1.0 |
| eval-runner image | rebuilt as **v4**, first build on the new deps |

### Judge determinism is approximate, not absolute

`judge_client` pins `temperature=0` "for reproducibility". Measured on the same gold
set: two panel runs gave **96.9%** and **100%** within tolerance (MAE 0.044 vs 0.041).

Concurrency is **not** the cause. A serialized run (`JUDGE_PANEL_MAX_WORKERS=1`)
returned 96.9% / MAE 0.044 / alpha 0.992 — *identical* to concurrent run 1. So the
jitter is model-side, and the concurrent panel is score-equivalent to the serial one.
Worth knowing before treating a 3-point calibration move as a regression.

### Still unverified — stated so it does not read as coverage

* **The router was not deployed.** `TierRoutingLlm`, the Claude tiers and
  `restore_tool_call_ids` are unexercised on ADK 2.8.0. The container also resolves
  **litellm 1.100.0** while we test against **1.96.2** (the `evaluation` extra caps us
  at `<1.97`) — a four-minor skew on the library the Claude tiers run through, and the
  one `restore_tool_call_ids` works around.
* **The MCP servers were not redeployed** despite `mcp` moving 1.29 → 1.30.
* **`simulated_eval` and `pairwise_eval`** were not run on 2.x.
* **GEPA (`run_optimize`)** was not run, and ADK 2.8.0 changed the evaluation module
  it depends on.
* **Model Armor templates let an evasive injection through** on the probe engine
  (recorded below) — a finding, not a failure.

### The armor layers, measured separately

Three prompts against the live probe (`gemini-2.5-flash`, so **templates** cover it and
the plugin correctly declines — no double-screening):

| Prompt | Handled by |
| --- | --- |
| benign policy question | answered correctly, no block (no over-blocking) |
| overt injection matching `BLOCKED_PATTERNS` | **client guardrail** |
| injection phrased to evade all four regexes | **nothing blocked it** — templates passed it through |

The plugin was then exercised on a Gemini-3 backbone, where templates cannot apply:

| Prompt | Handled by |
| --- | --- |
| benign policy question | answered, no block |
| evasive injection | **ADK plugin blocked** |
| overt injection | **ADK plugin blocked** |

So the plugin is not parity — it catches an injection that **both** the client
blocklist and the templates let through. That is the strongest argument for enabling
`ENABLE_MODEL_ARMOR_PLUGIN` on the Gemini-3 engines.

### Router validated on the new stack (2026-09-09)

The gap the first validation pass left open is now closed. Redeployed in place with
**every** tier override reproduced from the engine's own baked env — the router
redeploy trap regresses tiers to Gemini-3 on a plain `--update`, and the probe engine
had just taught us that an update also silently drops any opt-in flag missing from the
deploying shell. Reading the live env first and replaying it made the config diff
against pre-deploy **zero changed findings**: only the container moved.

`verify_router_health`: **14/14 FULL, 0.0% silent empty** (95% CI [0.0%, 21.5%]),
p50 5.7s / p95 30.6s, and 100% full on all four exercised tiers — lite, flash, pro and
the Claude `high` tier. VERDICT: PASS.

**litellm was pinned first, deliberately.** Unbounded, the container resolved 1.100.0
— and the Claude tiers run through litellm, under a workaround
(`src/models/tool_call_ids.py`) for a litellm/Anthropic/ADK interaction that only
reproduces in a mixed-tier session. Deploying both changes at once would have made a
failure unattributable.

**There is no single "version we test against."** The first pin said `>=1.93.0` to
match the local 1.96.2 and immediately reddened CI, which had installed **1.85.7**.
`google-cloud-aiplatform`'s `evaluation` extra caps litellm **by interpreter** —
`>=1.83.7,<1.86` under Python 3.12 (CI), `>=1.93,<1.97` under 3.14 (local dev). So
"pin to what we test" is not a well-defined instruction for this package. The upper
bound is the part that matters; the floor stays permissive so both environments
resolve. The repo's own guard
(`TestDeployedRequirementsMatchTheTestedEnvironment`) caught the mistake.

**A pre-check worth repeating, with an honest result.**
`src.eval.spike_tool_call_ids` — which reproduces the tool-call-id bug with no deploy —
now returns **INCONCLUSIVE**: on ADK 2.8.0 + litellm 1.96.2 the *pre-fix* arm no longer
reproduces the failure, and both arms return full Claude responses. The workaround is
harmless and still shipped, but the spike can no longer prove it is *needed*. Either
ADK stopped stripping the ids or the Anthropic path changed. Do not read INCONCLUSIVE
as "fixed" — the original bug required a mixed-tier session and specific conditions —
but do note the test has lost its power to validate the fix.

### MCP servers redeployed and verified from their own logs (2026-09-09)

The last unverified deploy surface. `mcp` moved 1.29 → 1.30 in this refresh and the
three Cloud Run servers had never been rebuilt on it.

**Scope discipline first.** The project also hosts `wrangler-search-mcp` /
`wrangler-booking-mcp` / `wrangler-expense-mcp`, which belong to a *different*
solution. Confirmed `deploy_mcp_servers.SERVERS` targets only our three, and
re-checked afterwards that the wrangler services were still on their original
revision (`00008`) and untouched.

Rolled `00006 → 00007` on all three, Ready=True. `.env` came back **byte-identical** —
the URLs are stable, so the verbatim env writer correctly no-ops.

**Verified from the servers' own Cloud Logging, not from the client's opinion:**

| Check | Result |
| --- | --- |
| `verify_mcp_tools` after redeploy | PASS — all 10 tools across 3 servers |
| Synthetic traffic (40 queries, 3 users) | **0 errors** |
| HTTP status across the whole window | **only 200 / 202** — zero 4xx, zero 5xx |
| Severity | 333 INFO, **zero ERROR/CRITICAL** |
| Revision actually serving | 100% on `*-00007-*` |

Two details worth reading rather than skimming:

* **No 404s.** A 404 on `/mcp` is what "Session terminated" looked like before
  `stateless_http=True`; its absence under real concurrent traffic is the evidence
  that fix still holds on mcp 1.30.
* **`INFO:mcp.server.streamable_http:Terminating session: None`** appears ~100× per
  server and is **correct**, not an error — stateless mode creates and tears down a
  session per request, so there is no session id to report.

**Transport health is not tool health**, so the trajectory was captured directly.
Green logs would look identical if every tool returned nothing:

| Server | Tool actually executed | Returned |
| --- | --- | --- |
| search-mcp | `search_flights` | honest "no flights for that date" from the mock DB |
| booking-mcp | `list_all_bookings` | real records (`BK-0FB370F2`) |
| expense-mcp | `check_expense_policy`, `get_user_expenses` | the real $150 limit; real records (`EX-203D7887`) |

Two prompts produced **no** tool call — the agent asked for a missing date instead.
That is agent behaviour on an under-specified request, not a server fault, and it is
why the confirming prompts had to be fully specified. Worth knowing before reading an
empty trajectory as a broken server.
