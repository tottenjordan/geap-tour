"""Stream-probe a deployed Agent Engine and report an honest PASS/FAIL.

The coordinator empty-stream outage (2026-08-13) has a specific signature: the
engine returns **HTTP 200 with zero events and no exception** — the managed
worker is SIGKILL'd at the first LLM call. The existing traffic tools cannot see
this: ``generate_traffic``'s error counter only trips on exceptions and
``_send_single_query`` returns ``True`` on an empty stream. So this module
streams **one** query and reports ``ok`` iff at least one event came back.

Usage:
    uv run python -m src.eval.probe_engine <ENGINE_ID|resource> [--message ...] \
        [--user-id ...] [--json]

Exit code is 0 when the engine streamed (``ok``), 1 otherwise — so it drops into
a shell ``&&`` chain around a deploy. ``probe_engine`` never raises: a stream
exception is captured in ``error``; an empty stream is ``ok=False, error=None``.
"""

from __future__ import annotations

import argparse
import json
import time

from src.config import GCP_PROJECT_ID, GCP_REGION
from src.traffic.generate_traffic import _extract_text

# A multi-intent prompt that exercises both sub-agents (travel + expense), so a
# healthy coordinator must delegate and stream tool + answer events.
DEFAULT_PROBE_MESSAGE = (
    "Find a flight from SFO to JFK next Monday and check if a $50 meal is within policy."
)


def probe_engine(
    engine,
    message: str,
    *,
    user_id: str = "probe",
    session_id: str | None = None,
) -> dict:
    """Stream one query at a deployed engine; count events. Never raises.

    Returns ``{events, text_events, ok, error, elapsed_s, first_event_s}``.
    ``ok`` is True iff ``events > 0``. An empty stream (HTTP 200, 0 events, no
    exception) is the documented coordinator-outage signature and reports
    ``ok=False, error=None``; a stream exception reports ``ok=False`` with the
    exception text in ``error``.
    """
    events = 0
    text_events = 0
    error: str | None = None
    first_event_s: float | None = None
    start = time.monotonic()
    try:
        if session_id is None:
            session_id = engine.create_session(user_id=user_id)["id"]
        for event in engine.stream_query(
            user_id=user_id,
            session_id=session_id,
            message=message,
        ):
            if first_event_s is None:
                first_event_s = time.monotonic() - start
            events += 1
            if _extract_text(event):
                text_events += 1
    except Exception as exc:  # probe must never raise; report the error text
        error = f"{type(exc).__name__}: {exc}"
    return {
        "events": events,
        "text_events": text_events,
        "ok": events > 0,
        "error": error,
        "elapsed_s": time.monotonic() - start,
        "first_event_s": first_event_s,
    }


def format_result(result: dict) -> str:
    """One-line human summary of a probe result (contains PASS/FAIL)."""
    verdict = "PASS" if result.get("ok") else "FAIL"
    parts = [
        f"{verdict}",
        f"events={result.get('events')}",
        f"text_events={result.get('text_events')}",
        f"elapsed={result.get('elapsed_s', 0.0):.2f}s",
    ]
    if result.get("error"):
        parts.append(f"error={result['error']}")
    elif not result.get("ok"):
        parts.append("empty-stream (200, 0 events, no exception)")
    return "  ".join(parts)


def _resolve_resource(engine_id: str) -> str:
    """Accept a bare engine id or a full resource name; return the resource name."""
    if engine_id.startswith("projects/"):
        return engine_id
    return f"projects/{GCP_PROJECT_ID}/locations/{GCP_REGION}/reasoningEngines/{engine_id}"


def _default_get_engine(resource: str):
    """Bind Vertex + fetch the engine (imported lazily so tests need no GCP)."""
    import vertexai
    from vertexai import agent_engines

    from src.config import disable_pyopenssl

    vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
    disable_pyopenssl()
    return agent_engines.get(resource)


def main(argv=None, *, get_engine=None) -> int:
    """CLI entry point. ``get_engine`` is injectable so tests never hit GCP."""
    parser = argparse.ArgumentParser(
        description="Stream-probe a deployed Agent Engine (PASS if events>0)"
    )
    parser.add_argument("engine", help="Engine id or full reasoningEngines resource name")
    parser.add_argument("--message", default=DEFAULT_PROBE_MESSAGE, help="Prompt to stream")
    parser.add_argument("--user-id", default="probe", help="user_id for the probe session")
    parser.add_argument("--json", action="store_true", help="Emit the result as JSON")
    args = parser.parse_args(argv)

    resource = _resolve_resource(args.engine)
    get_engine = get_engine or _default_get_engine
    engine = get_engine(resource)

    result = probe_engine(engine, args.message, user_id=args.user_id)
    result["engine"] = resource
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(format_result(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
