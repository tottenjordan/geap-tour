"""Tests for the evals-SDK patches (src/eval/_sdk_patches.py)."""

import json

from src.eval import _sdk_patches
from src.eval._sdk_patches import _extract_final_text, patch_evals_sdk


def test_extract_final_text_scans_past_tool_parts():
    """Final text is found even when the last event leads with a function_call."""
    resp = [
        {"content": {"parts": [{"function_call": {"name": "expense_agent", "id": "x__thought__abc"}}]}},
        {"content": {"parts": [{"function_response": {"name": "expense_agent", "id": "x__thought__abc"}}]}},
        {"content": {"parts": [
            {"function_call": {"name": "check", "id": "y"}},
            {"text": "The $50 meal is within policy."},
        ]}},
    ]
    assert _extract_final_text(resp) == "The $50 meal is within policy."


def test_extract_final_text_empty_when_no_text():
    """A pure tool-call turn (no synthesized answer) yields '' rather than raising."""
    resp = [
        {"content": {"parts": [{"function_call": {"name": "expense_agent", "id": "x"}}]}},
        {"content": {"parts": [{"function_response": {"name": "expense_agent", "id": "x"}}]}},
    ]
    assert _extract_final_text(resp) == ""


def test_extract_final_text_handles_empty_list():
    assert _extract_final_text([]) == ""


def test_patched_parser_recovers_text_instead_of_error_stub():
    """The patched parser returns real text where the original raised KeyError."""
    patch_evals_sdk()
    from vertexai._genai import _evals_common as ec

    resp = [
        {"content": {"parts": [{"function_call": {"name": "search", "id": "a"}}]}, "id": "e0", "author": "model"},
        {"content": {"parts": [{"function_response": {"name": "search", "id": "a"}}]}, "id": "e1", "author": "model"},
        {"content": {"parts": [{"text": "Here are your flights."}]}, "id": "e2", "author": "model"},
    ]
    response_row, intermediate, agent_data = ec._process_single_turn_agent_response(resp, None)
    assert response_row == "Here are your flights."
    # No "Failed to parse" error stub.
    assert "Failed to parse" not in json.dumps(response_row)
    assert len(intermediate) == 2  # all but the last event


def test_patch_is_idempotent():
    _sdk_patches._PATCHED = False
    patch_evals_sdk()
    patch_evals_sdk()  # second call is a no-op, must not raise
    assert _sdk_patches._PATCHED is True


def test_flip_extra_allows_error_field_on_result():
    """EvaluationItemResult loads candidate results carrying an error object."""
    patch_evals_sdk()
    from vertexai._genai import types as t

    data = {
        "candidateResults": [
            {
                "candidate": "agent_engine_0",
                "metric": "tool_use_quality_v1",
                "error": {"code": 3, "message": "no tool calls in trace"},
            }
        ]
    }
    result = t.EvaluationItemResult(**data)  # must not raise
    assert len(result.candidate_results or []) == 1
