"""Runtime patches for the Vertex AI evals SDK (vertexai._genai).

Two independent bugs surface when evaluating agents that run on Gemini 3.x
(thought-signature function calls) with the modern aiplatform/genai stack:

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

Call `patch_evals_sdk()` once before running inference/evaluation.
"""

import json

_PATCHED = False


def _flip_extra_to_ignore() -> int:
    """Flip extra='forbid' -> 'ignore' on all pydantic models in the eval types."""
    import pydantic
    from vertexai._genai import types as t

    models = [
        getattr(t, name)
        for name in dir(t)
        if isinstance(getattr(t, name), type)
        and issubclass(getattr(t, name), pydantic.BaseModel)
    ]
    flipped = 0
    for cls in models:
        if cls.model_config.get("extra") == "forbid":
            cls.model_config["extra"] = "ignore"
            flipped += 1
        cls.__pydantic_complete__ = False
    for cls in models:
        try:
            cls.model_rebuild(force=True)
        except Exception:
            pass
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
                        f"Failed to parse agent run response {str(resp_item)} to "
                        f"agent data: {e}"
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


def patch_evals_sdk() -> None:
    """Apply both evals-SDK patches (idempotent)."""
    global _PATCHED
    if _PATCHED:
        return
    _flip_extra_to_ignore()
    _patch_single_turn_parser()
    _PATCHED = True
