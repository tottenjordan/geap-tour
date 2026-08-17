# Type-checking (ty) baseline

`uv run ty check src/` is **not** wired into CI or a pre-commit hook — it's a
developer aid. This note records why a clean `ty` run still reports ~69
diagnostics and which of those are safe to ignore, so nobody wastes time
"fixing" untyped third-party surface or scatters `# type: ignore` across the
tree chasing a zero that isn't ours to reach.

## What was fixed

The genuinely-safe, behaviour-neutral diagnostics were corrected in code (no
`type: ignore`):

- `src/deploy/deploy_mcp_servers.py` — annotated `urls: dict[str, str]` and
  coerced the heterogeneous-config key with `str(server["name"])` (the key is a
  `str` at runtime; the `SERVERS` dicts just mix value types).
- `src/agents/coordinator/agent.py` + `src/router/agents.py` — the
  `save_memories_callback` default was `CallbackContext = None`; annotated
  `CallbackContext | None = None` to match the actual `None` default.

## What is intentionally left (and why)

The remaining diagnostics fall into a handful of untyped-SDK / environment
buckets. None are bugs; each would require either a `type: ignore` (rejected — we
don't scatter them) or restructuring code around SDKs that ship no useful stubs.

- **`unresolved-attribute` on Vertex `AgentEngine` / `AdkApp`** (the bulk) —
  `agent.stream_query(...)`, `agent.create_session(...)`,
  `agent_engines.get(...)`, `retrieve_memories`, etc. `vertexai.agent_engines`
  returns dynamically-shaped objects ty can't see through. These calls are
  exercised live and covered by tests with fakes.
- **`invalid-assignment` on ADK MCP connection params** (`timeout`,
  `sse_read_timeout` as `float*` against a `StdioConnectionParams | Sse... |
  StreamableHTTP...` union) in `coordinator/agent.py` and `registry.py` — ADK's
  connection-param protocol union; the assignment is valid at runtime.
- **`invalid-assignment` in `src/eval/_sdk_patches.py`** — this module's entire
  job is to monkeypatch ADK/Vertex internals (`_process_single_turn_agent_response`,
  `_execute_agent_run_with_retry`, `AGENT_MAX_WORKERS`); ty correctly notices the
  reassignments, which are deliberate.
- **`unresolved-import`** — optional / path / relative imports ty can't resolve:
  `OpenSSL.SSL` (guarded `try/except` in `config.py`), `otel_setup` and
  `.mock_db` in the MCP servers (resolved by the Cloud Run working dir), KFP
  `dsl` bits, and a `trace_v1 = None` optional-dependency fallback in
  `fetch_trace.py`.
- **`no-matching-overload` on `sum(...)`** — summing over values ty has widened
  to `object` off untyped SDK/dict data (`router_eval.py`,
  `multi_agent_batch_eval.py`).
- **`invalid-type-form` in `src/pipelines/components.py`** — the functional
  `NamedTuple("Out", [...])` return annotation is the **required** KFP component
  idiom (already carries `# type: ignore[valid-type]` for mypy-style checkers).
- **`invalid-assignment` on `litellm.suppress_debug_info = True`**
  (`router/agents.py`) — litellm types the attr as `Literal[False]`; setting it
  `True` is the documented way to quiet it.
- **stdlib / `.venv` diagnostics** (`builtins.pyi`, `statistics.pyi`,
  `vertexai/.../adk.py`) — not our code; nothing to fix.

## Re-checking

```bash
uv run ty check src/
```

If the count drops after an SDK upgrade ships stubs, prune the corresponding
bullet here. If a *new* diagnostic appears that is **not** in the buckets above,
treat it as a real finding and fix it rather than adding it to this list.
