# Memory Bank scope: memories are keyed by the ENGINE ID, not "coordinator_agent"

**TL;DR.** Vertex AI Agent Engine Memory Bank scopes memories by `{app_name, user_id}`.
On a *deployed* engine the runtime sets a session's `app_name` to **the engine's own
reasoning-engine id** — not the ADK agent's Python name. Reading or writing under
`app_name="coordinator_agent"` is a self-consistent but **wrong** scope: it silently
returns nothing that the live agent can see, and vice-versa. This is why the memory
demo appeared to "persist nothing" for months.

## How we found it (2026-08-17)

Seeding curated personas, then `verify_cross_session_recall`, kept printing
`RECALL: FAIL` even though `verify_memory` reported the facts as present. Two probes
disagreeing about the same user is the tell. Inspecting a live session:

```python
agent = agent_engines.get(_resolve_agent_resource_name("4380288848559603712"))
s = agent.create_session(user_id="alice")
# s["app_name"] == "4380288848559603712"   <-- the ENGINE ID, not "coordinator_agent"
```

So at runtime `PreloadMemoryTool` (read) and `add_session_to_memory` (write) both
scope by `app_name=<engine_id>`. Facts written under `app_name="coordinator_agent"`
(the old `verify_memory.DEFAULT_APP_NAME`) are invisible to the agent, and facts the
agent writes are invisible to a reader using `"coordinator_agent"`. Reading with
`app_name=None` (user-only) also returns nothing — the app_name **must** match.

Proof: writing alice's facts under `scope={"app_name": "<engine_id>", "user_id": "alice"}`
and probing a fresh session made the coordinator answer *"Delta / window seat /
Marriott corporate rate"* → `RECALL: PASS`.

## The fix

`src/eval/verify_memory.py` now defaults the memory scope `app_name` to the **bare
engine id** (`_bare_engine_id(engine_id)`), via an `_ENGINE_SCOPED` sentinel:

- No `app_name` given → scope by engine id (the runtime's scope). ← new default
- Explicit `app_name="..."` → use it verbatim.
- `app_name=None` → scope by user only (drops `app_name` from the scope).

`src/eval/seed_demo_memories.py` and `verify_cross_session_recall.py` inherit this
default, so seed → store → `PreloadMemoryTool` recall all agree.

## Why the "organic" path never persisted retrievable facts

The coordinator's `save_memories_callback` calls `add_session_to_memory()` inside a
`try/except Exception: pass`, so a failing/slow distillation write is invisible, and
its output (if any) lands under `app_name=<engine_id>` — which the old reader
(`"coordinator_agent"`) never queried. Net effect: `verify_memory` reported zero
facts from real traffic. The demo now **creates facts directly** via
`agent_engines.create_memory(...)` (synchronous, no async distillation, no
cold-engine empty-stream) under the correct engine-id scope. See
`docs/notes/geap-demo-provisioning.md`.

## Gotchas that compound this (already documented elsewhere)

- `agent_engines.get` needs the FULL resource name **and** `vertexai.init(location=GCP_REGION)`
  first — see the memory `agent-engines-get-needs-full-name-and-region-init`.
- The probe engine streams an empty 200 on cold start / heavy prompts; a
  *booking* probe ("book me a flight…") empties out, so the recall demo now uses a
  pure-recall probe ("remind me of my saved travel preferences") and retries on an
  empty stream (`--probe-attempts`, default 3).
