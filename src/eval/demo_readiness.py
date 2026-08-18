"""Pre-demo readiness check — one command, PASS/FAIL per demo surface.

Consolidates the scattered ``verify_*`` checks into a single "green board" a
presenter runs at T-5min so nobody demos into a broken surface. It composes the
existing verifiers (does NOT reimplement them):

* **mcp_tools** — the three toolsets resolve their real tools
  (:func:`src.eval.verify_mcp_tools.run_checks`). *critical*
* **engine_live** — the probe engine returns a non-empty response, retried past
  cold-start (reuses :func:`src.eval.tool_faithfulness.capture_interaction`). This
  is the direct guard for the empty-at-200 cold-start failure documented in
  ``docs/notes/online-quality-monitor.md``. *critical*
* **memory_store** — the Memory Bank has persisted persona facts
  (:func:`src.eval.verify_memory.fetch_memories`). *critical*
* **monitors** — the three monitoring surfaces report ``status: ok``
  (:func:`src.eval.verify_monitors.verify_monitor_results`). *advisory* — an
  intentional demo regression point (e.g. the faithfulness RED publish) or a
  single-sample low-confidence dip should not block the gate.
* **cross_session_recall** — genuine session-A→session-B recall works
  (:func:`src.eval.verify_cross_session_recall.run_cross_session_recall`). Opt-in
  via ``--deep`` because it drives live billable sessions and polls for async
  fact distillation (slow). *critical when included.*

Exit 0 iff every *critical* check passes, so it can gate a demo script::

    uv run python -m src.eval.demo_readiness --engine-id <ENGINE_ID>
    uv run python -m src.eval.demo_readiness --engine-id <ENGINE_ID> --deep --json

Every check is injectable, so ``tests/test_demo_readiness.py`` exercises the whole
compose → render → gate path with no GCP.
"""

from __future__ import annotations

import argparse
import json as _json
from collections.abc import Callable, Sequence

# One simple, tool-free prompt — a booking probe streams empty on the probe engine
# (see docs/notes/geap-demo-provisioning.md), so keep the liveness probe plain text.
DEFAULT_LIVE_PROMPT = "In one sentence, what can you help me with?"
DEFAULT_USER_ID = "alice"


# --------------------------------------------------------------------------- #
# Individual checks — each returns ``(ok: bool, detail: str)``
# --------------------------------------------------------------------------- #
def check_mcp_tools(*, run_checks_fn: Callable[[], list[dict]] | None = None) -> tuple[bool, str]:
    """The three MCP toolsets resolve their real tools."""
    if run_checks_fn is None:
        from src.eval.verify_mcp_tools import run_checks as run_checks_fn
    results = run_checks_fn()
    n_ok = sum(1 for r in results if r.get("ok"))
    ok = bool(results) and n_ok == len(results)
    return ok, f"{n_ok}/{len(results)} toolsets resolved"


def check_monitors(
    *, verify_fn: Callable[..., dict] | None = None, hours: int = 24
) -> tuple[bool, str]:
    """The three monitoring surfaces report an overall healthy status."""
    if verify_fn is None:
        from src.eval.verify_monitors import verify_monitor_results as verify_fn
    data = verify_fn(output_format="json", hours=hours) or {}
    status = data.get("status", "unknown")
    return status == "ok", f"status={status}"


def check_memory(
    *, engine_id: str, user_id: str, fetch_fn: Callable[..., list[str]] | None = None
) -> tuple[bool, str]:
    """The Memory Bank has persisted facts for the demo persona."""
    if fetch_fn is None:
        from src.eval.verify_memory import fetch_memories as fetch_fn
    facts = fetch_fn(user_id, engine_id=engine_id)
    return len(facts) > 0, f"{len(facts)} persisted facts for '{user_id}'"


def _get_engine(engine_id: str):
    """Resolve a deployed-engine handle (full name + regional init — see memory)."""
    import vertexai
    from vertexai import agent_engines

    from src.config import GCP_REGION
    from src.eval.batch_eval import _resolve_agent_resource_name

    vertexai.init(location=GCP_REGION)
    return agent_engines.get(_resolve_agent_resource_name(engine_id))


def check_engine_live(
    *,
    engine_id: str,
    engine=None,
    capture_fn: Callable[..., dict] | None = None,
    attempts: int = 3,
    prompt: str = DEFAULT_LIVE_PROMPT,
) -> tuple[bool, str]:
    """The engine returns a non-empty response, retried past cold-start.

    Reuses :func:`src.eval.online_monitor.capture_live_interactions` — the proven
    live path that creates a session per call before ``stream_query``. That
    function transparently falls back to the raw-SSE reader when the SDK's
    array-only parser can't read a recycled engine's NDJSON stream, so this check
    reports genuine liveness (a true empty-at-200 / wedge) rather than a
    false-negative SDK parse skew. ``capture_fn`` returns ``[(prompt, response), ...]``.
    """
    if capture_fn is None:
        from src.eval.online_monitor import capture_live_interactions as capture_fn
    eng = engine if engine is not None else _get_engine(engine_id)
    last_err = ""
    for i in range(1, attempts + 1):
        # A cold stream can raise (error-shaped response) OR return empty text; the
        # online monitor buckets both as infra-empty. Treat either as a retryable
        # miss so a transient cold-start blip doesn't fail the whole gate.
        try:
            pairs = capture_fn(eng, [prompt]) or []
            response = (pairs[0][1] if pairs else "").strip()
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            continue
        if response:
            return True, f"non-empty response ({len(response)} chars) on attempt {i}/{attempts}"
    suffix = f"; last error: {last_err}" if last_err else ""
    return False, f"empty-at-200 on all {attempts} attempts (cold-start / wedged){suffix}"


def check_recall(
    *, engine_id: str, user_id: str, recall_fn: Callable[..., dict] | None = None
) -> tuple[bool, str]:
    """Genuine cross-session recall (session A → new session B) works."""
    if recall_fn is None:
        from src.eval.verify_cross_session_recall import run_cross_session_recall as recall_fn
    result = recall_fn(user_id, engine_id=engine_id)
    ok = bool(result.get("recalled"))
    return ok, "recalled" if ok else "no recall"


# --------------------------------------------------------------------------- #
# Composition / gate
# --------------------------------------------------------------------------- #
def build_default_checks(*, engine_id: str, user_id: str, deep: bool = False) -> list[dict]:
    """Assemble the real check list (each entry: name, critical, run thunk)."""
    checks = [
        {"name": "mcp_tools", "critical": True, "run": lambda: check_mcp_tools()},
        {
            "name": "engine_live",
            "critical": True,
            "run": lambda: check_engine_live(engine_id=engine_id),
        },
        {
            "name": "memory_store",
            "critical": True,
            "run": lambda: check_memory(engine_id=engine_id, user_id=user_id),
        },
        {"name": "monitors", "critical": False, "run": lambda: check_monitors()},
    ]
    if deep:
        checks.append(
            {
                "name": "cross_session_recall",
                "critical": True,
                "run": lambda: check_recall(engine_id=engine_id, user_id=user_id),
            }
        )
    return checks


def _safe(run: Callable[[], tuple[bool, str]]) -> tuple[bool, str]:
    """Run a check thunk, turning any exception into a red (ok=False) result."""
    # A broken surface must render a red row, not crash the whole readiness gate.
    try:
        return run()
    except Exception as exc:
        return False, f"error: {type(exc).__name__}: {exc}"


def run_readiness(
    *,
    engine_id: str | None = None,
    user_id: str = DEFAULT_USER_ID,
    deep: bool = False,
    checks: list[dict] | None = None,
) -> list[dict]:
    """Run each check and return a list of ``{name, ok, critical, detail}`` rows."""
    if checks is None:
        checks = build_default_checks(engine_id=engine_id or "", user_id=user_id, deep=deep)
    results = []
    for check in checks:
        ok, detail = _safe(check["run"])
        results.append(
            {"name": check["name"], "ok": ok, "critical": check["critical"], "detail": detail}
        )
    return results


def is_ready(results: list[dict]) -> bool:
    """True iff every *critical* check passed (advisory failures are ignored)."""
    return all(r["ok"] for r in results if r["critical"])


def render(results: list[dict]) -> str:
    """A PASS/FAIL board, most-legible for a presenter glancing pre-demo."""
    lines = []
    for r in results:
        mark = "PASS" if r["ok"] else "FAIL"
        tag = "" if r["critical"] else " (advisory)"
        lines.append(f"  [{mark}] {r['name']}{tag}: {r['detail']}")
    verdict = "READY" if is_ready(results) else "NOT READY"
    lines.append(f"\nDEMO READINESS: {verdict}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None, *, checks: list[dict] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pre-demo green-board readiness check.")
    parser.add_argument("--engine-id", default=None, help="Probe/coordinator engine id.")
    parser.add_argument(
        "--user-id", default=DEFAULT_USER_ID, help="Memory persona (default: alice)."
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Also run cross-session recall (live, billable, slow).",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    engine_id = args.engine_id
    if checks is None and engine_id is None:
        from src.eval.verify_memory import _default_engine_id

        engine_id = _default_engine_id()

    results = run_readiness(
        engine_id=engine_id, user_id=args.user_id, deep=args.deep, checks=checks
    )
    if args.json:
        print(_json.dumps(results, indent=2))
    else:
        print(render(results))
    return 0 if is_ready(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
