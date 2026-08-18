# Traffic Generator Session Reuse Implementation Plan

> **For Claude:** Execute task-by-task with TDD. After each task run the modern-python
> gate: `uv run ruff format --check && uv run ruff check && uv run ty check src/ && uv run pytest`.
> **NO `Co-Authored-By` / "Generated with Claude Code"** trailers on commits or PRs.
> Git identity: Jordan Totten `<jordantotten@google.com>`. Do NOT touch
> `notebooks/jt_eval_jw.ipynb`. NEVER repoint `.env`. This work is **local-only /
> no-cloud** — every test uses a fake agent; do not deploy or drive a live engine.
> Do NOT merge the PR without review.

**Goal:** Make the traffic generator reuse one session per user across many queries
instead of calling `create_session` once per query, so a single engine can sustain
far higher QPS without hitting the shared session-creation ceiling.

**Architecture:** Introduce a small thread-safe `SessionPool` (one cached session id
per `user_id`, created lazily). Thread `session_pool` through `_send_single_query` and
its callers (`generate_steady_traffic`, `generate_load`, `generate_scaling_profile`).
Reuse defaults ON with a `--no-reuse-sessions` opt-out. A reused session that goes
stale is invalidated and retried once. All existing per-query behavior is preserved
when no pool is passed (backward compatible for current tests/callers).

**Tech Stack:** Python 3.11+, `threading` (ThreadPoolExecutor is already used by
`generate_load`), pytest with fakes, uv/ruff/ty per the modern-python skill.

---

## Background (why this is the fix)

Discovered 2026-08-18: scaling one engine to `min_instances=4` did **not** relieve
`Failed to create session` 400s (56 failures at 3 QPS) — the bottleneck is the shared
managed session-creation path, and the generator opens a **fresh session per query**
(`src/traffic/generate_traffic.py:372`, `:250`, `:344`), so sustainable QPS ≈ the
`create_session` rate. Reusing sessions per user cuts `create_session` calls from
once-per-query to once-per-user (3 users → 3 calls total instead of thousands). See
memory `session-creation-ceiling-not-fixed-by-replicas` and
`docs/notes/` (add a note in Task 6).

**Existing precedent to mirror:** the multi-turn conversation path already reuses one
`conv_session_id` across turns (`generate_traffic`, `:271-282`). This generalizes that
idea to all traffic modes.

## Pieces to reuse (do NOT reimplement)

- `_send_single_query(agent, query, user_id, complexity) -> bool`
  (`src/traffic/generate_traffic.py:363`) — the single choke point every non-burst mode
  goes through (steady `:452`, load `:535`). Its raw-SSE `ValueError` fallback
  (`:381-393`) must stay untouched.
- `FakeAgent` / `ConcurrencyAgent` / `FakeClock` test stubs
  (`tests/test_traffic_load.py:17-70`) — reuse verbatim for new tests.
- `generate_load(..., session_pool=?, reuse_sessions=?)` signature style — it already
  takes injectable `sleep`/`monotonic`/`seed`/`metrics_writer`, so add the new params in
  the same keyword-only block (`:479-497`).

---

## Task 1: `SessionPool` — thread-safe one-session-per-user cache

**Files:**
- Modify: `src/traffic/generate_traffic.py` (add class near `_send_single_query`, ~line 360)
- Test: `tests/test_traffic_session_reuse.py` (create)

**Step 1: Write the failing test**

```python
# tests/test_traffic_session_reuse.py
import threading

from src.traffic.generate_traffic import SessionPool


class CountingAgent:
    """Counts create_session calls; returns a per-user session id."""

    def __init__(self):
        self.create_calls = []
        self._lock = threading.Lock()

    def create_session(self, user_id=None):
        with self._lock:
            self.create_calls.append(user_id)
        return {"id": f"sess-{user_id}-{len(self.create_calls)}"}


def test_pool_reuses_one_session_per_user():
    agent = CountingAgent()
    pool = SessionPool(agent)
    sids = [pool.get("alice") for _ in range(5)]
    assert len(set(sids)) == 1  # same session every time
    assert agent.create_calls == ["alice"]  # created exactly once


def test_pool_one_session_per_distinct_user():
    agent = CountingAgent()
    pool = SessionPool(agent)
    pool.get("alice")
    pool.get("bob")
    pool.get("alice")
    pool.get("charlie")
    assert sorted(agent.create_calls) == ["alice", "bob", "charlie"]


def test_pool_invalidate_forces_recreate():
    agent = CountingAgent()
    pool = SessionPool(agent)
    first = pool.get("alice")
    pool.invalidate("alice")
    second = pool.get("alice")
    assert first != second
    assert agent.create_calls == ["alice", "alice"]


def test_pool_thread_safe_single_create_under_concurrency():
    agent = CountingAgent()
    pool = SessionPool(agent)
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()  # maximize contention
        pool.get("alice")

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert agent.create_calls == ["alice"]  # lock => created exactly once
```

**Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_traffic_session_reuse.py -v`
Expected: FAIL with `ImportError: cannot import name 'SessionPool'`.

**Step 3: Minimal implementation**

```python
# src/traffic/generate_traffic.py  (add above _send_single_query)
import threading  # add to imports if not present


class SessionPool:
    """Reuse one session per user across many queries.

    ``create_session`` is the throughput ceiling (see docs/plans/2026-08-18-
    traffic-session-reuse.md): calling it once per query caps sustainable QPS at
    the session-creation rate. This caches one session id per ``user_id`` so N
    queries for a user cost 1 ``create_session``. Thread-safe because
    ``generate_load`` dispatches on a ThreadPoolExecutor.
    """

    def __init__(self, agent):
        self._agent = agent
        self._sessions: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, user_id: str) -> str:
        with self._lock:
            sid = self._sessions.get(user_id)
            if sid is None:
                sid = self._agent.create_session(user_id=user_id)["id"]
                self._sessions[user_id] = sid
            return sid

    def invalidate(self, user_id: str) -> None:
        with self._lock:
            self._sessions.pop(user_id, None)
```

**Step 4: Run to verify pass**

Run: `uv run pytest tests/test_traffic_session_reuse.py -v`
Expected: PASS (4 tests).

**Step 5: Commit**

```bash
git add src/traffic/generate_traffic.py tests/test_traffic_session_reuse.py
git commit -m "feat(traffic): add thread-safe SessionPool (one session per user)"
```

---

## Task 2: `_send_single_query` uses the pool + self-heals a stale session

**Files:**
- Modify: `src/traffic/generate_traffic.py:363` (`_send_single_query`)
- Test: `tests/test_traffic_session_reuse.py`

**Step 1: Write the failing test**

```python
from src.traffic.generate_traffic import _send_single_query


class PoolAgent(CountingAgent):
    """create_session (counted) + a stream_query that records the session used."""

    def __init__(self):
        super().__init__()
        self.streamed = []  # (session_id, message)

    def stream_query(self, *, user_id, session_id, message):
        self.streamed.append((session_id, message))
        return iter([{"text": "ok"}])


def test_send_single_query_reuses_pool_session():
    agent = PoolAgent()
    pool = SessionPool(agent)
    for i in range(4):
        assert _send_single_query(agent, f"q{i}", "alice", "low", session_pool=pool)
    assert agent.create_calls == ["alice"]  # one session for 4 queries
    assert len({s for s, _ in agent.streamed}) == 1  # all on the same session


def test_send_single_query_without_pool_is_unchanged():
    agent = PoolAgent()
    for i in range(3):
        assert _send_single_query(agent, f"q{i}", "alice", "low")
    assert agent.create_calls == ["alice", "alice", "alice"]  # per-query (legacy)


class StaleOnceAgent(PoolAgent):
    """First stream_query on any session raises a stale-session error, then works."""

    def __init__(self):
        super().__init__()
        self._failed_sessions = set()

    def stream_query(self, *, user_id, session_id, message):
        if session_id not in self._failed_sessions:
            self._failed_sessions.add(session_id)
            raise RuntimeError("404 Session not found or terminated")
        return iter([{"text": "ok"}])


def test_stale_session_invalidates_and_retries_once():
    agent = StaleOnceAgent()
    pool = SessionPool(agent)
    # First call: sid#1 fails stale -> invalidate -> sid#2 also "first use" fails...
    # so make the AGENT only fail the FIRST distinct session:
    ok = _send_single_query(agent, "q", "alice", "low", session_pool=pool)
    assert ok is True
    assert len(agent.create_calls) == 2  # recreated once after the stale error
```

> Note: tune `StaleOnceAgent` so exactly the first session id is stale and the
> recreated one succeeds (retry-once semantics). Keep the retry to **one** attempt.

**Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_traffic_session_reuse.py -k "pool or stale" -v`
Expected: FAIL — `_send_single_query` has no `session_pool` kwarg yet.

**Step 3: Minimal implementation**

Add a stale-session classifier and rewrite `_send_single_query` keeping the existing
`ValueError` raw-SSE fallback intact:

```python
_STALE_SESSION_MARKERS = ("session not found", "session terminated", "no session", "404")


def _is_stale_session(exc: Exception) -> bool:
    return any(m in str(exc).lower() for m in _STALE_SESSION_MARKERS)


def _send_single_query(agent, query, user_id, complexity, *, session_pool=None) -> bool:
    try:
        if session_pool is not None:
            session_id = session_pool.get(user_id)
        else:
            session_id = agent.create_session(user_id=user_id)["id"]
        for _chunk in agent.stream_query(user_id=user_id, session_id=session_id, message=query):
            pass
        return True
    except ValueError as e:
        # existing raw-SSE parse-skew fallback — unchanged
        from src.eval import raw_stream

        resource = raw_stream.agent_resource_name(agent)
        if not raw_stream.is_sse_parse_skew(e) or not resource:
            print(f"  x Error: {e}")
            return False
        try:
            raw_stream.capture_pairs(resource, [query], user_id=user_id)
            return True
        except Exception as raw_e:
            print(f"  x Error (raw fallback): {raw_e}")
            return False
    except Exception as e:
        # A reused session may have gone stale — drop it and retry once fresh.
        if session_pool is not None and _is_stale_session(e):
            session_pool.invalidate(user_id)
            try:
                session_id = session_pool.get(user_id)
                for _chunk in agent.stream_query(
                    user_id=user_id, session_id=session_id, message=query
                ):
                    pass
                return True
            except Exception as e2:
                print(f"  x Error (after session refresh): {e2}")
                return False
        print(f"  x Error: {e}")
        return False
```

**Step 4: Run to verify pass**

Run: `uv run pytest tests/test_traffic_session_reuse.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add src/traffic/generate_traffic.py tests/test_traffic_session_reuse.py
git commit -m "feat(traffic): _send_single_query reuses a pooled session, self-heals stale"
```

---

## Task 3: Wire the pool into `generate_steady_traffic`

**Files:**
- Modify: `src/traffic/generate_traffic.py:399` (`generate_steady_traffic`)
- Test: `tests/test_traffic_session_reuse.py`

**Step 1: Write the failing test**

`generate_steady_traffic` fetches its own agent via `agent_engines.get`, so the test
monkeypatches that and `vertexai.init`, and drives one interval with a fake clock is
overkill — instead assert create_session count via a patched agent over a tiny run.

```python
import src.traffic.generate_traffic as gt


def _patch_engine(monkeypatch, agent):
    monkeypatch.setattr(gt.vertexai, "init", lambda **_: None)
    monkeypatch.setattr(
        gt, "_resolve_engine_resource", lambda a, d: "projects/x/reasoningEngines/1"
    )
    monkeypatch.setattr(gt.agent_engines, "get", lambda name: agent)


def test_steady_reuses_sessions(monkeypatch):
    agent = PoolAgent()
    _patch_engine(monkeypatch, agent)
    # 0-minute duration would send nothing; use a short real duration with a big
    # interval so exactly one batch of queries goes out. Patch time to end fast:
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    gt.generate_steady_traffic(duration_minutes=0, interval_seconds=1, queries_per_interval=3)
    # duration 0 -> loop may not run; adjust helper to guarantee one batch, then:
    # every query for a given user shares one session:
    assert len(agent.create_calls) <= len(set(agent.create_calls))  # <= distinct users
```

> The duration/loop mechanics make an exact-count assertion fiddly; prefer to test the
> **reuse path directly** (Task 2 already proves per-user reuse) and here assert only
> that `generate_steady_traffic` **passes a `SessionPool`** into `_send_single_query`.
> Do that by monkeypatching `gt._send_single_query` to record its `session_pool` kwarg:

```python
def test_steady_passes_a_session_pool(monkeypatch):
    agent = PoolAgent()
    _patch_engine(monkeypatch, agent)
    monkeypatch.setattr(gt.time, "sleep", lambda s: None)
    seen = {}

    def spy(a, q, u, c, *, session_pool=None):
        seen["pool"] = session_pool
        return True

    monkeypatch.setattr(gt, "_send_single_query", spy)
    gt.generate_steady_traffic(duration_minutes=1, interval_seconds=999, queries_per_interval=1)
    assert isinstance(seen["pool"], gt.SessionPool)
```

**Step 2: Run — FAIL** (`generate_steady_traffic` passes no pool yet).

**Step 3: Implementation** — after `agent = agent_engines.get(...)` add
`pool = SessionPool(agent) if reuse_sessions else None`, add a `reuse_sessions: bool = True`
param to the signature, and pass `session_pool=pool` at the `_send_single_query` call
(`:452`).

**Step 4: Run — PASS.**

**Step 5: Commit**
```bash
git commit -am "feat(traffic): steady mode reuses a per-user SessionPool"
```

---

## Task 4: Wire the pool into `generate_load` (+ shared pool for scaling)

**Files:**
- Modify: `src/traffic/generate_traffic.py:479` (`generate_load`) and `:635`
  (`generate_scaling_profile`)
- Test: `tests/test_traffic_session_reuse.py`

**Step 1: Write the failing test** (reuse `FakeClock` from `tests/test_traffic_load.py`)

```python
from tests.test_traffic_load import FakeClock  # or copy the stub


def test_generate_load_reuse_caps_create_sessions():
    agent = PoolAgent()
    clock = FakeClock()
    summary = gt.generate_load(
        agent,
        target_qps=5,
        duration_s=2.0,
        ramp_s=0,
        workers=4,
        user_pool=["alice", "bob", "charlie"],
        seed=1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        reuse_sessions=True,
    )
    assert summary["sent"] > 3
    # create_session called at most once per distinct user, NOT once per query:
    assert set(agent.create_calls) <= {"alice", "bob", "charlie"}
    assert len(agent.create_calls) <= 3


def test_generate_load_no_reuse_is_per_query():
    agent = PoolAgent()
    clock = FakeClock()
    summary = gt.generate_load(
        agent,
        target_qps=5,
        duration_s=2.0,
        ramp_s=0,
        workers=4,
        user_pool=["alice"],
        seed=1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        reuse_sessions=False,
    )
    assert len(agent.create_calls) == summary["sent"]  # one session per query


def test_scaling_shares_one_pool_across_stages():
    agent = PoolAgent()
    clock = FakeClock()
    stages = [{"qps": 3, "duration_s": 1.0}, {"qps": 3, "duration_s": 1.0}]
    gt.generate_scaling_profile(
        agent,
        stages=stages,
        workers=4,
        seed=1,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    # Two stages but sessions persist across them -> still <= distinct users:
    assert len(agent.create_calls) <= 3
```

> Check `generate_scaling_profile`'s real signature (`:635`) for how it forwards
> `sleep`/`monotonic`/`seed` to `generate_load`; match it. If it doesn't currently
> forward a clock, add the same injectable params it already accepts.

**Step 2: Run — FAIL** (no `reuse_sessions`/`session_pool` on `generate_load`).

**Step 3: Implementation**
- `generate_load(..., session_pool=None, reuse_sessions=True)`. Near the top:
  `pool = session_pool if session_pool is not None else (SessionPool(agent) if reuse_sessions else None)`.
  In `_do_send`, call `_send_single_query(agent, message, user, complexity, session_pool=pool)`.
- `generate_scaling_profile`: build `pool = SessionPool(scaling_agent)` once and pass
  `session_pool=pool` to each per-stage `generate_load(...)` so sessions persist across
  stages.

**Step 4: Run — PASS**, then the whole load/scaling suite:
`uv run pytest tests/test_traffic_load.py tests/test_traffic_scaling.py tests/test_traffic_session_reuse.py -v`
(Existing load/scaling tests count `stream_query`, not `create_session`, so they stay
green with reuse defaulting ON — confirm.)

**Step 5: Commit**
```bash
git commit -am "feat(traffic): load + scaling reuse sessions (shared pool across stages)"
```

---

## Task 5: CLI `--no-reuse-sessions` opt-out

**Files:**
- Modify: `src/traffic/generate_traffic.py` arg parser (~`:750`) and the
  `--scaling`/`--load`/`--steady` dispatch (`:819-856`)
- Test: `tests/test_traffic_session_reuse.py`

**Step 1: Write the failing test** — parse args and assert the flag flips a boolean.
Prefer asserting the dispatch forwards `reuse_sessions=False` by spying on
`gt.generate_steady_traffic` (monkeypatch) with `--no-reuse-sessions` on `argv`.

```python
def test_cli_no_reuse_flag_forwards_false(monkeypatch):
    seen = {}
    monkeypatch.setattr(gt, "generate_steady_traffic", lambda **kw: seen.update(kw))
    monkeypatch.setattr(
        gt.sys, "argv", ["prog", "ENG", "--steady", "--no-reuse-sessions", "--duration", "1"]
    )
    gt.main() if hasattr(gt, "main") else gt._cli()  # match the real entrypoint
    assert seen["reuse_sessions"] is False
```

> Inspect how the module is invoked (`if __name__ == "__main__":` block at the bottom)
> and target the real entrypoint. If arg parsing is inline under `__main__`, refactor the
> dispatch into a `main(argv=None)` first (small, testable) — otherwise spy at the
> `generate_*` boundary via a `--dry`-style parse. Keep the refactor minimal.

**Step 2–4:** add
`parser.add_argument("--no-reuse-sessions", dest="reuse_sessions", action="store_false")`
with `parser.set_defaults(reuse_sessions=True)`, and pass `reuse_sessions=args.reuse_sessions`
into the steady/load/scaling calls. Run the test → PASS.

**Step 5: Commit**
```bash
git commit -am "feat(traffic): --no-reuse-sessions CLI opt-out (reuse on by default)"
```

---

## Task 6: Docs

**Files:**
- Create: `docs/notes/traffic-session-reuse.md`
- Modify: `CLAUDE.md` (traffic/eval area — one bullet), the existing note that mentions
  the load generator if any.

Content: the ceiling finding (session-creation rate, not compute), the one-session-per-
user design, thread-safety, stale-session self-heal, reuse-default-ON + opt-out, and the
before/after (create_session per query → per user). Link memory
`session-creation-ceiling-not-fixed-by-replicas`.

**Commit**
```bash
git add docs/notes/traffic-session-reuse.md CLAUDE.md
git commit -m "docs(traffic): session reuse note + CLAUDE.md bullet"
```

---

## Verification (final)

```bash
uv run ruff format --check && uv run ruff check && uv run ty check src/
uv run pytest tests/test_traffic_session_reuse.py tests/test_traffic_load.py \
             tests/test_traffic_scaling.py tests/test_traffic_resolve.py -q
uv run pytest    # full suite green
```

Optional live smoke (needs GCP; probe engine, no `.env` change) — proves the ceiling
lifts. Compare create-session errors at the SAME QPS that failed before (3 QPS):
```bash
uv run python -m src.traffic.generate_traffic 4380288848559603712 --load --qps 3 \
    --duration 2 --workers 8 --emit-metrics --label demo=session-reuse
# expect: 0 (or near-0) "Failed to create session" vs 56 previously
```

## Success criteria
- `SessionPool` reuses one session per user, thread-safe, unit-tested with fakes.
- All non-burst modes (steady/load/scaling) reuse sessions by default; scaling shares
  one pool across stages; `--no-reuse-sessions` restores per-query behavior.
- A stale reused session is invalidated + retried once.
- Existing `test_traffic_load.py` / `test_traffic_scaling.py` stay green (no behavior
  change they assert on).
- Full suite + ruff + ty green. `.env` untouched; no notebook touched.

## Caveats (state in the docs note)
- **Burst mode** (`generate_traffic`, `:216-265`) is left per-query for the single-shot
  path except its conversation branch, which already reuses a session; only the non-burst
  modes get the pool. (Extend later if burst needs high volume.)
- **Raw-SSE fallback** (`raw_stream.capture_pairs`) still creates its own session
  internally; pooling that path is future work (it only triggers on the parse-skew
  engine, not the hot path).
- Reuse changes traffic *shape* (fewer distinct sessions) — fine for load/throughput
  demos; if a demo specifically needs many distinct sessions, use `--no-reuse-sessions`.
