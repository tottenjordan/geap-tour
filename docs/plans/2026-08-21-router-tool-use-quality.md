# Fix `tool_use_quality_v1` for the deployed router

> **For Claude:** Execute task-by-task with TDD. Gate every commit on
> `uv sync --all-groups && uv run ruff format --check && uv run ruff check && uv run ty check src/ && uv run pytest`.
> **NO `Co-Authored-By` / "Generated with Claude Code"** trailers. Git identity:
> Jordan Totten `<jordantotten@google.com>`. Do NOT touch `notebooks/jt_eval_jw.ipynb`.
> Do NOT commit `eval_output/`. NEVER repoint `.env`. Engines are updated **in place
> only** — never recreated. Commit/push and open PRs only when explicitly asked.
> **First action:** copy this file to `docs/plans/2026-08-21-router-tool-use-quality.md`
> and commit it, so the plan lives with the repo.

**Goal:** Make `tool_use_quality_v1` score the router again — and when it legitimately
can't, fail with a message that names the reason instead of an opaque service error.

**Architecture:** Three independent defects stacked into one symptom. Diagnose first
(one read-only probe), then fix the eval wiring, the stale descriptor, and the
error legibility. Only the diagnosis gates the rest.

**Tech Stack:** `agentplatform._genai` evals SDK, ADK 2.7.1, pytest.

---

## Context

Running the deployed **router** through the batch eval produced:

> `tool_use_quality_v1 requires tool calls in the evaluation trace, but no
> function_call/function_response events were found. To use this metric, ensure the
> model makes tool calls during the evaluated interaction.`

**What the message actually means.** The metric is not scored from the response text.
It is scored from the `AgentData` our own patched parser builds out of the engine's
`stream_query` event list — `src/eval/_sdk_patches.py:137-193` assembles
`AgentData(turns=[ConversationTurn(events=[AgentEvent(...)])], agents=…)`. The rubric
needs at least one `function_call` / `function_response` part somewhere in those
events. If **every** item in the run is tool-free, the metric cannot be computed at
all, so the service errors instead of returning a low score. One tool-using item
would have been enough to produce a number.

**This is a regression, and there is proof.** `eval_outputs/batch_results_multi_agent_eval_20260812_072222.json`
records `router_agent` → `tool_use_quality_v1 = 0.542` over 12 cases. It worked on
2026-08-12.

**Why it broke.** On 2026-08-12 the router was the `transfer_to_agent` + `sub_agents`
design, and `transfer_to_agent` *is* a `function_call` — so every routed turn emitted
a tool event by construction, regardless of whether a domain tool ran. The
2026-08-19/20 rearchitecture (`docs/notes/router-transfer-streaming.md`) replaced that
with **one direct-tools agent** that swaps its model per tier
(`src/router/agents.py:298-313`, `tools=[*_mcp_tools(), _memory_tool()]`, no
`sub_agents`). That removed the guaranteed function_call. Now a tool event appears
only if the tier model actually chooses to call an MCP tool — and on
`ROUTER_EVAL_CASES`, which are mostly `low_complexity`, the turn is served by the
**lite** tier (`gemini-2.5-flash-lite`).

Confirmed by inspection, not assumed:

| finding | evidence |
| --- | --- |
| the router does hold real tools | `src/router/agents.py:307` |
| its lite instruction tells it to use them | `src/agents/lite_agent.py:37` "Always use the appropriate tools when a query requires data retrieval" |
| the eval cases do expect tools | every `ROUTER_EVAL_CASES` entry has `expected_tool`, e.g. `search_mcp_search_flights` |
| but some live turns are tool-free | this morning's `verify_router_health` shows turns with `events=2, chars=58` (no tool round-trip) alongside `events=4` (likely one) |
| and the logs show no tool execution | 2h of router logs: MCP toolset construction and `create_session`, zero tool-execution lines |

So the leading hypothesis is that the lite tier answers these short prompts from its
instruction rather than calling a tool — **but that is a hypothesis, and Task 0
settles it before any code changes.**

**Two further defects found while tracing this.** Both are real regardless of what
Task 0 says:

1. **`_build_router_info()` still describes the deleted architecture.**
   `src/eval/agent_eval_configs.py:396` declares a router that "delegates by prompt
   complexity" with `sub_agents=["lite_agent","flash_agent","pro_agent","sonnet_agent","opus_agent"]`.
   Those tier agents are no longer reachable from the root agent. This descriptor is
   handed to the eval service as `AgentData.agents`, so the judge is being told about
   an architecture that no longer runs.
2. **`AgentConfig.tools` exists and is populated nowhere.** The field is real
   (`['agent_id','agent_type','description','instruction','tools','sub_agents']`) and
   *every* descriptor in `agent_eval_configs.py` omits it — coordinator, router,
   travel, expense. The eval service is never told what tools the agent has. This is
   a plausible contributor to the coordinator's chronically low ~0.36-0.42 as well.

**A third, separate footgun** (not the cause here — the user confirmed they passed
`--agent-id 6134…`): `multi_agent_batch_eval` resolves **one** engine for **all**
requested agents (`src/eval/multi_agent_batch_eval.py:262`), defaulting to
`AGENT_ENGINE_ID`, which in `.env` is `3639024497392091136` = `coordinator_agent_jt1`,
a **coordinator**. So a bare `multi_agent_batch_eval` — or `run_all_evals`, which
calls it with no `--agent-id` — scores `ROUTER_EVAL_CASES` against a coordinator
engine. Silently.

**Intended outcome:** the router's tool-use score is either a real number or an error
that names which of the three causes fired.

---

## Existing pieces to reuse (do NOT reimplement)

- `src/eval/spike_trajectory_visibility.py` — purpose-built for exactly the Task 0
  question ("what tool calls does a deployed engine surface client-side?"), already
  takes `--agent-id` and `--prompt`, already has the raw-SSE skew fallback.
- `src/eval/trajectory_eval.py:capture_trajectory(events)` — returns
  `[{tool_name, tool_input, returned}]` from an event stream. The preflight in Task 2
  should count tool events with this, not a fresh parser.
- `src/config.py:ROUTER_ENGINE_ID` / `AGENT_ENGINE_ID` — the agent→engine map in
  Task 3 reads these; do not add new env vars.
- `src/eval/_sdk_patches.py:_extract_final_text` — the existing event-walking helper;
  match its defensive `(event or {}).get("content") or {}` style.

---

## Task 0 — Diagnose (read-only, gates everything else)

No code changes. This decides whether Task 5 is needed.

**Step 1.** Probe the router with a prompt that must use a tool:

```bash
uv run python -m src.eval.spike_trajectory_visibility \
  --agent-id 6134089059699523584 \
  --prompt "Find flights from SFO to JFK"
```

**Step 2.** Repeat with the multi-step default prompt (forces ≥2 domain tools), which
routes to a higher tier:

```bash
uv run python -m src.eval.spike_trajectory_visibility --agent-id 6134089059699523584
```

**Step 3.** Record which of these you see, because it selects the fix:

| observation | meaning | action |
| --- | --- | --- |
| `function_call` parts present in both | the router *does* call tools; the eval-time turns were empty or the run hit the wrong engine | Tasks 1-4 only; skip Task 5 |
| tool calls on the multi-step prompt, none on the simple one | the **lite tier answers without tools** — the eval cases are all low-complexity, so the whole run is tool-free | Tasks 1-5, Task 5 is the real fix |
| no tool calls on either | the direct-tools router is not calling MCP at all — a live agent bug, not an eval bug | **stop and report**; this plan does not cover it |

Note: driving the router fires its `after_agent_callback` (`save_memories_callback`),
so this writes a Memory Bank fact for the probe user. Harmless, but it is not a pure
read.

**Step 4.** Commit the plan copy (`docs/plans/2026-08-21-router-tool-use-quality.md`)
with the Task 0 findings appended as a short "Diagnosis" section.

---

## Task 1 — Declare tools on every agent descriptor

**Files:** `src/eval/agent_eval_configs.py` · Test: `tests/test_eval_configs.py`
(check the real filename first; reuse the existing eval-config test module)

**Step 1 — failing test.** Every descriptor must declare a non-empty `tools` list,
and the router's must not claim sub-agents:

```python
def test_every_agent_config_declares_tools():
    for name in ALL_AGENTS:
        info = build_agent_info(name)
        for agent_id, cfg in info.agents.items():
            assert cfg.tools, f"{name}/{agent_id} declares no tools"

def test_router_descriptor_is_direct_tools():
    """The router stopped delegating on 2026-08-20 — no sub_agents survive."""
    info = build_agent_info("router_agent")
    assert list(info.agents) == ["router_agent"]
    assert info.agents["router_agent"].sub_agents == []
```

**Step 2 — run:** `uv run pytest tests/test_eval_configs.py -q` → both FAIL.

**Step 3 — implement.** Add one shared tool-name constant near the eval cases and
reference it from each builder rather than repeating literals. Derive the names from
the `expected_tool` values already in the case lists (`search_mcp_search_flights`,
`search_mcp_search_hotels`, `booking_mcp_book_flight`, `expense_mcp_check_expense_policy`,
`expense_mcp_submit_expense`, …) — grep the case lists for the full set rather than
guessing. Rewrite `_build_router_info()` as a single direct-tools agent whose
description/instruction match `src/router/agents.py` (one agent, five backbones,
tier chosen by a complexity classifier, no delegation).

**Step 4 — run:** tests PASS.

**Step 5 — commit.** `fix(eval): declare agent tools and correct the stale router descriptor`

## Task 2 — Make the failure legible

**Files:** `src/eval/multi_agent_batch_eval.py` · Test: `tests/test_multi_agent_eval.py`

A whole eval run currently dies on an opaque service error. Count the tool events
*before* spending the eval, and say what was found.

**Step 1 — failing test.** Add a pure helper, `_count_tool_call_items(df) -> tuple[int, int]`
(items with ≥1 tool event, total items), so it is unit-testable with a hand-built
DataFrame — no cloud:

```python
def test_counts_items_with_tool_calls():
    df = pd.DataFrame({"agent_data": [
        _agent_data_with_tool("search_flights"),
        _agent_data_text_only(),
    ]})
    assert _count_tool_call_items(df) == (1, 2)
```

**Step 2 — run:** FAIL (helper does not exist).

**Step 3 — implement.** Between `run_inference` and `create_evaluation_run`, read
`inference_result.eval_dataset_df` (confirm the agent-data column name from the df at
runtime — the patched parser returns `(response, intermediate_events, agent_data)`),
count with `trajectory_eval.capture_trajectory`, then:

- `0` of `N` items have tool calls → print
  `tool_use_quality: 0/N items made tool calls — metric skipped (see docs/notes/…)`
  and drop `TOOL_USE_QUALITY` from `metrics` for this run. The other five still score.
- `>0` → proceed unchanged, but print the count so a low score is interpretable.

**Step 4 — run:** `uv run pytest tests/test_multi_agent_eval.py -q` → PASS.

**Step 5 — commit.** `fix(eval): skip tool-use scoring with a clear reason when no turn called a tool`

## Task 3 — Per-agent engine routing

**Files:** `src/eval/multi_agent_batch_eval.py` · Test: `tests/test_multi_agent_eval.py`

**Step 1 — failing test.** `router_agent` must resolve to `ROUTER_ENGINE_ID`, not
`AGENT_ENGINE_ID`, and an explicit `--agent-id` must still win for every agent:

```python
def test_router_resolves_to_router_engine():
    assert _engine_for_agent("router_agent", None).endswith(ROUTER_ENGINE_ID)
    assert _engine_for_agent("coordinator_agent", None).endswith(AGENT_ENGINE_ID)

def test_explicit_agent_id_overrides_the_map():
    assert _engine_for_agent("router_agent", "999").endswith("999")
```

**Step 2 — run:** FAIL.

**Step 3 — implement.** Add `_engine_for_agent(agent_name, agent_id)` and move the
resolution from `run_multi_agent_batch_eval` (currently line 262, once for the whole
run) **into the per-agent loop**. Print the resolved engine per agent — a run that
scores two agents on two engines must say so. Keep `--agent-id` as an explicit
override for all agents (the bake-off depends on that).

**Step 4 — run:** full suite.

**Step 5 — commit.** `fix(eval): resolve the engine per agent instead of once per run`

## Task 4 — Verify against the live router

```bash
uv run python -m src.eval.multi_agent_batch_eval \
  --agents router_agent --agent-id 6134089059699523584 --limit 4
```

Expect either a real `tool_use_quality_v1` score **or** the new explicit
`0/N items made tool calls` line — never the opaque service error. Then run without
`--agents` to confirm the coordinator path is unchanged and now prints two different
engines.

## Task 5 — CONDITIONAL: only if Task 0 showed the lite tier skipping tools

Do **not** do this speculatively.

**Files:** `src/eval/agent_eval_configs.py` (`ROUTER_EVAL_CASES`)

The router's case list is weighted to `low_complexity` single-fact prompts, which is
correct for measuring *routing*, but it means the tool-use rubric is being asked
about turns where not calling a tool may be the right behavior. Add 3-4
tool-obligatory cases (a booking, an expense submission, a multi-step search) so at
least some items exercise the tool path, keeping the existing low-complexity cases
for the routing metrics. Do not delete cases — `src/eval/dataset_integrity.py` and
the complexity-accuracy eval read this list.

If instead the tier instruction is the problem, note it and stop: changing
`src/agents/lite_agent.py:INSTRUCTION` moves the *served* router's behavior and
belongs in its own change with its own before/after eval, not bundled here.

## Task 6 — Docs

**Files:** create `docs/notes/router-tool-use-quality.md`; one index line in
`docs/notes/README.md` (currently 189 lines, cap 200); cross-link from
`docs/notes/coordinator-tool-use-quality.md`, which covers the sibling
delegation-blind false-negative.

Record: what the error actually measures (`AgentData` events, not response text); the
0.542-on-2026-08-12 proof that it regressed; why removing `transfer_to_agent` removed
the guaranteed function_call; the unused `AgentConfig.tools` field; and the
one-engine-for-all-agents footgun.

---

## Verification

```bash
uv sync --all-groups && uv run ruff format --check && uv run ruff check \
  && uv run ty check src/ && uv run pytest -q     # expect 1132+ passed

# The reported failure, fixed
uv run python -m src.eval.multi_agent_batch_eval \
  --agents router_agent --agent-id 6134089059699523584 --limit 4

# The coordinator path did not regress (compare to the 2026-08-21 baseline:
# match .72 / quality .73 / halluc .63 / instr .63 / safety .96 / tooluse .38)
uv run python -m src.eval.multi_agent_batch_eval \
  --agents coordinator_agent --agent-id 4380288848559603712

# Per-agent routing is visible, not silent
uv run python -m src.eval.multi_agent_batch_eval --limit 2   # prints an engine per agent
```

## Success criteria

- `--agents router_agent --agent-id 6134…` yields a real `tool_use_quality_v1` score,
  or an explicit `0/N items made tool calls` line naming the reason.
- `build_agent_info("router_agent")` describes one direct-tools agent with a populated
  `tools` list and no `sub_agents`.
- `multi_agent_batch_eval` with no `--agents` sends router cases to the router engine.
- Coordinator rubric scores stay within their existing spread; `tool_use_quality` for
  the coordinator does not silently change meaning.
- Full suite green; no `.env` edits; `eval_output/` uncommitted.

## Caveats

- **Task 0 gates the plan.** If the router calls no tools on either probe, that is a
  live agent defect and this eval-side plan is the wrong fix — stop and report.
- **Declaring `tools` may move the coordinator's score.** It has sat at 0.36-0.42
  across seven runs; giving the judge a real tool inventory could legitimately raise
  it. That is a *meaning* change to a monitored series (`tool_use_accuracy` via
  `publish_offline_eval`) — record the before/after in the note and do not
  retroactively compare across it.
- **`geap_tool_use` is not the fix here.** `src/eval/tool_use_judge.py` is
  coordinator-specific (it imports `EVAL_CASES` from `batch_eval`) and scores
  `(prompt, final-response-text)` with no trajectory, so it cannot answer "did a tool
  actually run".
- The `hallucination` level shift from the ADK 2.7.1 upgrade is still open; do not
  read coordinator rubric movement in this work as caused by these changes.
