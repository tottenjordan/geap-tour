# Traffic generator reuses one session per user (`SessionPool`)

**Date:** 2026-08-18 · **Context:** `Failed to create session` 400s under load.

## Problem — sustainable QPS was capped at the session-creation rate, not compute

The traffic generator opened a **fresh session per query** — `create_session`
on every request (`src/traffic/generate_traffic.py`, in `_send_single_query`
and each mode's send path). So sustainable QPS was bounded by the *shared,
managed* session-creation rate, **not** per-replica compute. Scaling one engine
to `min_instances=4` did **not** relieve it: 56 `Failed to create session` 400s
at 3 QPS on 2026-08-18. See memory
`session-creation-ceiling-not-fixed-by-replicas` — the real levers are session
reuse, fan-out across distinct engines, or low QPS.

## Fix — a thread-safe `SessionPool`, one session per `user_id`

`SessionPool` (`src/traffic/generate_traffic.py`) caches one session id per
`user_id`, created **lazily under a lock** (thread-safe because `generate_load`
dispatches on a `ThreadPoolExecutor`):

- `get(user_id)` → returns the cached session id, calling `create_session` only
  on first use for that user.
- `invalidate(user_id)` → drops the cached session so the next `get` recreates.

The pool is threaded through `_send_single_query(..., session_pool=)` and reused
by all three non-burst modes — `generate_steady_traffic`, `generate_load`, and
`generate_scaling_profile`. The **scaling profile shares ONE pool across all
stages**, so sessions persist across stages rather than being recreated per
stage. This generalizes the pattern the multi-turn conversation path already
used (one session reused across turns).

## Behavior

- Reuse is **ON by default**; `--no-reuse-sessions` (CLI, `dest=reuse_sessions`,
  `action=store_false`) restores the legacy per-query behavior. Passing no pool
  to `_send_single_query` is also unchanged (backward compatible).
- **Stale-session self-heal:** if a reused session goes stale — matched by
  `_is_stale_session` (`"session not found" / "session terminated" / "no
  session" / "404"`) — it is invalidated and the query is retried **once** on a
  fresh session.
- The existing raw-SSE `ValueError` fallback (`raw_stream`) in
  `_send_single_query` is untouched.

## Before / after

`create_session` calls drop from **once per query** to **once per user**: e.g. a
3-user pool makes **3** `create_session` calls total instead of thousands.

## Caveats

- **Burst mode** keeps its single-shot path per-query; only its conversation
  branch (which already reused a session) reuses. Only the non-burst modes get
  the pool.
- The **raw-SSE fallback** (`raw_stream.capture_pairs`) still creates its own
  session internally — pooling that path is future work (it only triggers on the
  parse-skew engine, not the hot path).
- Reuse changes traffic **shape** (fewer distinct sessions) — fine for
  load/throughput demos; if a demo specifically needs many distinct sessions,
  use `--no-reuse-sessions`.
