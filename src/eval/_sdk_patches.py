"""Runtime patches for the Vertex AI evals SDK (vertexai._genai).

Four independent issues surface when evaluating agents that run on Gemini 3.x
(thought-signature function calls) on a deployed Agent Engine:

1. Response parsing (`_evals_common._process_single_turn_agent_response`):
   the SDK extracts the final text as `resp_item[-1]["content"]["parts"][0]["text"]`,
   assuming the last event's first part is text. With Gemini 3.x the final event's
   first part is often a `function_call`/`function_response` (no `text` key), and a
   pure-tool turn can yield an empty event list — both raise, and the SDK stores a
   "Failed to parse agent run response" stub as the response, collapsing every
   metric to ~0. Fix: scan events last→first for the actual text part; treat a
   missing final text as an empty response instead of an error.

2. Result loading (`types.EvaluationItemResult`): the model uses `extra="forbid"`,
   so result JSON containing a candidate `error` object fails to load
   ("Failed to load evaluation result from GCS"). Fix: flip `extra` to "ignore"
   on the eval types (the same approach already used in simulated_eval and
   run_optimize) so unknown/extra fields are tolerated.

3. Inference concurrency (`_evals_common.AGENT_MAX_WORKERS = 20`): the SDK fans
   every prompt out at once (20 concurrent `stream_query` calls). A cold or
   single-instance engine drops ~half of them, returning empty turns that can't
   be scored — this alone tanked coordinator `tool_use_quality` (10/20 items
   dropped; a serial warm re-run recovered 9/10). Fix: throttle the agent worker
   pool to a safe default (env `EVAL_AGENT_MAX_WORKERS`, default 4).

4. Empty-turn no-retry (`_evals_common._execute_agent_run_with_retry`): its retry
   loop only retries on *exceptions* — when `stream_query` completes normally but
   yields no content events, it returns `[]` immediately with no retry, so a
   transient empty turn becomes a permanent unscored item. Fix: wrap it to treat
   an empty list as transient and retry with backoff (env `EVAL_EMPTY_RETRIES`,
   default 4).

Call `patch_evals_sdk()` once before running inference/evaluation. Optionally call
`warm_agent_engine()` first to spin the engine up before the batched fan-out.
"""

import contextlib
import json
import os
import time

_PATCHED = False

# Cap concurrent stream_query fan-out at the deployed engine (SDK default is 20).
_AGENT_MAX_WORKERS = int(os.environ.get("EVAL_AGENT_MAX_WORKERS", "4"))
# How many times to re-run an item whose turn came back empty (no content events).
_EMPTY_RETRIES = int(os.environ.get("EVAL_EMPTY_RETRIES", "4"))
# Base backoff (seconds) between empty-turn retries; grows linearly with attempt.
_EMPTY_BACKOFF = float(os.environ.get("EVAL_EMPTY_BACKOFF", "2.0"))


def _is_empty_turn(result) -> bool:
    """True when an agent run returned no content events (an empty turn).

    The SDK's per-item inference returns a list of content events on success or a
    ``{"error": ...}`` dict on failure. An empty list means the engine's stream
    completed without yielding any content — the transient drop we retry on.
    """
    return isinstance(result, list) and len(result) == 0


def _run_with_empty_retry(fn, retries: int, sleep_fn) -> object:
    """Call ``fn`` until it returns a non-empty result, up to ``retries`` times.

    ``fn`` is a zero-arg callable returning the SDK's per-item inference result.
    Errors (dicts) and non-empty event lists are returned as-is; only empty turns
    (see :func:`_is_empty_turn`) trigger a retry. ``sleep_fn(attempt)`` is called
    between attempts (injectable so tests can run without sleeping).
    """
    last = None
    for attempt in range(retries):
        last = fn()
        if not _is_empty_turn(last):
            return last
        if attempt < retries - 1:
            sleep_fn(attempt)
    return last


def _flip_extra_to_ignore() -> int:
    """Flip extra='forbid' -> 'ignore' on all pydantic models in the eval types."""
    import pydantic
    from vertexai._genai import types as t

    models = [
        getattr(t, name)
        for name in dir(t)
        if isinstance(getattr(t, name), type) and issubclass(getattr(t, name), pydantic.BaseModel)
    ]
    flipped = 0
    for cls in models:
        if cls.model_config.get("extra") == "forbid":
            cls.model_config["extra"] = "ignore"
            flipped += 1
        cls.__pydantic_complete__ = False
    for cls in models:
        with contextlib.suppress(Exception):
            cls.model_rebuild(force=True)
    return flipped


def _extract_final_text(resp_item: list) -> str:
    """Scan agent-run events last->first for the final text part.

    Returns the last text part of the last event that has one, or "" if none
    (e.g. a turn that ended on a tool call with no synthesized answer).
    """
    for event in reversed(resp_item):
        parts = ((event or {}).get("content") or {}).get("parts") or []
        found = None
        for part in parts:
            if isinstance(part, dict) and part.get("text"):
                found = part["text"]
        if found is not None:
            return found
    return ""


def _patch_single_turn_parser() -> None:
    """Replace the response parser with a text-part-scanning version."""
    from vertexai._genai import _evals_common as ec

    types = ec.types
    genai_types = ec.genai_types

    def _patched(resp_item, agent_data_agents):
        intermediate_events_row: list = []
        response_row = None
        agent_data_row = None

        if isinstance(resp_item, list):
            try:
                response_row = _extract_final_text(resp_item)
                for intermediate_event in resp_item[:-1]:
                    intermediate_events_row.append(
                        {
                            "event_id": intermediate_event.get("id"),
                            "content": intermediate_event.get("content"),
                            "creation_timestamp": intermediate_event.get("timestamp"),
                            "author": intermediate_event.get("author"),
                        }
                    )
                agent_events = []
                for event_dict in resp_item:
                    content_dict = event_dict.get("content")
                    content_obj = None
                    if content_dict:
                        content_obj = genai_types.Content.model_validate(content_dict)
                    agent_events.append(
                        types.evals.AgentEvent(
                            author=event_dict.get("author", "model"),
                            content=content_obj,
                        )
                    )
                turn = types.evals.ConversationTurn(
                    turn_index=0,
                    turn_id="turn_0",
                    events=agent_events,
                )
                agent_data_row = types.evals.AgentData(
                    turns=[turn],
                    agents=agent_data_agents,
                ).model_dump(exclude_unset=True)
            except Exception as e:  # pylint: disable=broad-exception-caught
                error_payload = {
                    "error": (
                        f"Failed to parse agent run response {resp_item!s} to agent data: {e}"
                    ),
                }
                response_row = json.dumps(error_payload)
                agent_data_row = json.dumps(error_payload)
        elif isinstance(resp_item, dict) and "error" in resp_item:
            response_row = json.dumps(resp_item)
        else:
            error_payload = {
                "error": "Unexpected response type from agent run",
                "response_type": str(type(resp_item)),
                "details": str(resp_item),
            }
            response_row = json.dumps(error_payload)

        return response_row, intermediate_events_row, agent_data_row

    ec._process_single_turn_agent_response = _patched


def _throttle_agent_concurrency() -> None:
    """Cap the agent inference worker pool (SDK default AGENT_MAX_WORKERS=20)."""
    from vertexai._genai import _evals_common as ec

    ec.AGENT_MAX_WORKERS = _AGENT_MAX_WORKERS


def _patch_retry_on_empty() -> None:
    """Wrap agent-engine inference to retry empty turns (SDK retries only errors)."""
    from vertexai._genai import _evals_common as ec

    orig = ec._execute_agent_run_with_retry

    def _wrapped(row, contents, agent_engine, max_retries: int = 3):
        return _run_with_empty_retry(
            lambda: orig(row, contents, agent_engine, max_retries=max_retries),
            retries=_EMPTY_RETRIES,
            sleep_fn=lambda attempt: time.sleep(_EMPTY_BACKOFF * (attempt + 1)),
        )

    ec._execute_agent_run_with_retry = _wrapped


def warm_agent_engine(agent_engine, n: int = 2, message: str = "ping") -> int:
    """Send a few throwaway queries to spin the engine up before batched inference.

    Returns the number of warmup queries that returned at least one content event.
    Best-effort: swallows errors so a warmup failure never blocks the eval.
    """
    warmed = 0
    for i in range(n):
        try:
            got_content = False
            for event in agent_engine.stream_query(user_id=f"warmup-{i}", message=message):
                if event and (event.get("content") or {}).get("parts"):
                    got_content = True
            warmed += int(got_content)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    return warmed


def patch_evals_sdk() -> None:
    """Apply all evals-SDK patches (idempotent)."""
    global _PATCHED
    if _PATCHED:
        return
    _flip_extra_to_ignore()
    _patch_single_turn_parser()
    _throttle_agent_concurrency()
    _patch_retry_on_empty()
    _PATCHED = True
