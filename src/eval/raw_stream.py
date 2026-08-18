"""Raw-SSE Agent Engine stream client (client-only fallback for a SDK-parse skew).

**Why this exists.** The installed ``google-api-core`` (2.34.0 — the latest
available; ``>=2.35`` is unsatisfiable) ships an *array-only* REST streaming
parser (:mod:`google.api_core._rest_streaming_base`: it raises unless the HTTP
body starts with ``[``). A recycled Agent Engine streams **newline-delimited
JSON objects** via ``:streamQuery?alt=sse`` — each line a complete ``{...}``
event — so the SDK's ``agent.stream_query`` raises
``ValueError: Can only parse array of JSON objects, instead got {`` *even though
the engine is healthy* (verified live: raw HTTP 200 with a real gemini-2.5-flash
answer and a full function_call trajectory). There is no SDK flag to switch the
parser and no version to upgrade to.

This module sidesteps the SDK entirely: it POSTs to ``:streamQuery?alt=sse``
directly and parses the object-per-line stream itself, yielding the **same event
dicts** (``{"content": {"parts": [...]}, "author": ..., ...}``) the SDK would
have. So :func:`src.traffic.generate_traffic._extract_text` and the
:mod:`src.eval.trajectory_eval` extractors consume the events unchanged.

Everything here is **client-side** — no redeploy, the served engine is
untouched. The HTTP ``post`` and the auth ``token`` are injectable so the whole
path is unit-testable without GCP. See docs/notes/agent-engine-sse-stream-parse.md.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# Default user id for eval/monitor captures (mirrors online_monitor).
_DEFAULT_USER_ID = "online-monitor-user"

# The array-only google-api-core REST parser raises this exact message on an
# engine that streams NDJSON via :streamQuery?alt=sse (a recycled engine does).
# Detecting it lets a caller fall back to this raw-SSE client instead of failing
# a healthy engine.
SSE_PARSE_MARKER = "Can only parse array of JSON objects"


def is_sse_parse_skew(exc: BaseException) -> bool:
    """True for the array-parser-vs-NDJSON skew (not any other ValueError)."""
    return isinstance(exc, ValueError) and SSE_PARSE_MARKER in str(exc)


def agent_resource_name(agent) -> str | None:
    """Full ``projects/.../reasoningEngines/<id>`` of a deployed-engine handle."""
    name = getattr(agent, "resource_name", None)
    if name:
        return name
    api = getattr(agent, "api_resource", None)
    return getattr(api, "name", None)


def _endpoint_base(resource_name: str) -> str:
    """Regional aiplatform v1 base URL derived from the engine's resource name.

    ``resource_name`` is the full ``projects/<p>/locations/<region>/reasoningEngines/<id>``;
    the region is read straight from it so the endpoint and the resource always
    agree (never the ``global`` model endpoint — engines are regional).
    """
    parts = resource_name.split("/")
    region = parts[3] if len(parts) > 3 else "us-central1"
    return f"https://{region}-aiplatform.googleapis.com/v1"


def _default_token() -> str:
    """ADC bearer token (works from this env's application-default credentials)."""
    import google.auth
    import google.auth.transport.requests as gart

    creds, _ = google.auth.default()
    creds.refresh(gart.Request())
    return creds.token


def _default_post(url: str, *, headers: dict, json_body: dict, stream: bool):
    """Real HTTP POST (kept thin + injectable so tests never touch the network)."""
    import requests

    return requests.post(url, headers=headers, json=json_body, stream=stream, timeout=120)


def parse_sse_line(line: str | None) -> dict | None:
    """Parse one NDJSON/SSE line into an event dict, or ``None`` to skip it.

    Tolerant of an optional ``data:`` SSE prefix, blank lines, and a ``[DONE]``
    sentinel. Only top-level JSON *objects* are events (a bare array line, which
    is what the array-parser expects, is deliberately skipped).
    """
    if not line:
        return None
    line = line.strip()
    if line.startswith("data:"):
        line = line[len("data:") :].strip()
    if not line or line == "[DONE]":
        return None
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _iter_json_lines(resp: Any) -> Iterator[dict]:
    for raw in resp.iter_lines(decode_unicode=True):
        obj = parse_sse_line(raw)
        if obj is not None:
            yield obj


def create_session(
    resource_name: str,
    user_id: str,
    *,
    token: str | None = None,
    post=None,
) -> str:
    """Create a session on the engine and return its id (raw ``:query`` call)."""
    token = token or _default_token()
    post = post or _default_post
    base = _endpoint_base(resource_name)
    resp = post(
        f"{base}/{resource_name}:query",
        headers={"Authorization": f"Bearer {token}"},
        json_body={"class_method": "create_session", "input": {"user_id": user_id}},
        stream=False,
    )
    return resp.json()["output"]["id"]


def stream_query_events(
    resource_name: str,
    *,
    message: str,
    user_id: str,
    session_id: str,
    token: str | None = None,
    post=None,
) -> list[dict]:
    """Stream one turn and return the parsed event dicts (same shape as the SDK)."""
    token = token or _default_token()
    post = post or _default_post
    base = _endpoint_base(resource_name)
    resp = post(
        f"{base}/{resource_name}:streamQuery?alt=sse",
        headers={"Authorization": f"Bearer {token}"},
        json_body={
            "class_method": "stream_query",
            "input": {"user_id": user_id, "session_id": session_id, "message": message},
        },
        stream=True,
    )
    return list(_iter_json_lines(resp))


def capture_pairs(
    resource_name: str,
    prompts: Iterable[str],
    user_id: str = _DEFAULT_USER_ID,
    *,
    token: str | None = None,
    post=None,
) -> list[tuple[str, str]]:
    """``[(prompt, visible_response_text), ...]`` — drop-in for capture_live_interactions.

    One fresh session per prompt (matches the SDK-based capture). The auth token
    is fetched once and reused across all prompts.
    """
    from src.traffic.generate_traffic import _extract_text

    token = token or _default_token()
    pairs: list[tuple[str, str]] = []
    for prompt in prompts:
        sid = create_session(resource_name, user_id, token=token, post=post)
        events = stream_query_events(
            resource_name, message=prompt, user_id=user_id, session_id=sid, token=token, post=post
        )
        pairs.append((prompt, "".join(_extract_text(e) for e in events)))
    return pairs


def capture_triples(
    resource_name: str,
    prompts: Iterable[str],
    user_id: str = _DEFAULT_USER_ID,
    *,
    include_transfers: bool = False,
    token: str | None = None,
    post=None,
) -> list[dict]:
    """Like :func:`capture_pairs` but retains the executed trajectory.

    Returns ``[{"prompt", "response", "actual_trajectory"}, ...]`` — the shape
    :mod:`src.eval.tool_faithfulness` consumes (response via ``_extract_text``,
    trajectory via ``capture_trajectory``), captured in one stream pass.
    """
    from src.eval.trajectory_eval import capture_trajectory
    from src.traffic.generate_traffic import _extract_text

    token = token or _default_token()
    out: list[dict] = []
    for prompt in prompts:
        sid = create_session(resource_name, user_id, token=token, post=post)
        events = stream_query_events(
            resource_name, message=prompt, user_id=user_id, session_id=sid, token=token, post=post
        )
        out.append(
            {
                "prompt": prompt,
                "response": "".join(_extract_text(e) for e in events),
                "actual_trajectory": capture_trajectory(
                    events, include_transfers=include_transfers
                ),
            }
        )
    return out
