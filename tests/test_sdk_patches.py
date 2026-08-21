"""Tests for the evals-SDK patches (src/eval/_sdk_patches.py)."""

import json

from src.eval import _sdk_patches
from src.eval._sdk_patches import (
    _extract_final_text,
    _is_empty_turn,
    _run_with_empty_retry,
    patch_evals_sdk,
    warm_agent_engine,
)


def test_extract_final_text_scans_past_tool_parts():
    """Final text is found even when the last event leads with a function_call."""
    resp = [
        {
            "content": {
                "parts": [{"function_call": {"name": "expense_agent", "id": "x__thought__abc"}}]
            }
        },
        {
            "content": {
                "parts": [{"function_response": {"name": "expense_agent", "id": "x__thought__abc"}}]
            }
        },
        {
            "content": {
                "parts": [
                    {"function_call": {"name": "check", "id": "y"}},
                    {"text": "The $50 meal is within policy."},
                ]
            }
        },
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
    from agentplatform._genai import _evals_common as ec

    resp = [
        {
            "content": {"parts": [{"function_call": {"name": "search", "id": "a"}}]},
            "id": "e0",
            "author": "model",
        },
        {
            "content": {"parts": [{"function_response": {"name": "search", "id": "a"}}]},
            "id": "e1",
            "author": "model",
        },
        {"content": {"parts": [{"text": "Here are your flights."}]}, "id": "e2", "author": "model"},
    ]
    response_row, intermediate, _ = ec._process_single_turn_agent_response(resp, None)
    assert response_row == "Here are your flights."
    # No "Failed to parse" error stub.
    assert "Failed to parse" not in json.dumps(response_row)
    assert len(intermediate) == 2  # all but the last event


def test_patch_is_idempotent():
    _sdk_patches._PATCHED = False
    patch_evals_sdk()
    patch_evals_sdk()  # second call is a no-op, must not raise
    assert _sdk_patches._PATCHED is True


def test_is_empty_turn():
    """Only an empty event list is an empty turn; errors and content are not."""
    assert _is_empty_turn([]) is True
    assert _is_empty_turn([{"content": {"parts": [{"text": "hi"}]}}]) is False
    assert _is_empty_turn({"error": "boom"}) is False  # error dict is not retried
    assert _is_empty_turn(None) is False


def test_run_with_empty_retry_retries_until_content():
    """An empty turn is retried; the first non-empty result is returned."""
    results = [[], [], [{"content": {"parts": [{"text": "ok"}]}}]]
    calls = {"n": 0}
    slept = []

    def fn():
        r = results[calls["n"]]
        calls["n"] += 1
        return r

    out = _run_with_empty_retry(fn, retries=4, sleep_fn=lambda a: slept.append(a))
    assert out == [{"content": {"parts": [{"text": "ok"}]}}]
    assert calls["n"] == 3  # two empties then content
    assert slept == [0, 1]  # slept between the two retries only


def test_run_with_empty_retry_gives_up_after_retries():
    """All-empty results return the last (empty) value without exceeding retries."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return []

    out = _run_with_empty_retry(fn, retries=3, sleep_fn=lambda a: None)
    assert out == []
    assert calls["n"] == 3


def test_run_with_empty_retry_returns_error_without_retry():
    """An error dict is returned immediately (already retried internally)."""
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        return {"error": "resource exhausted"}

    out = _run_with_empty_retry(fn, retries=4, sleep_fn=lambda a: None)
    assert out == {"error": "resource exhausted"}
    assert calls["n"] == 1


def test_throttle_lowers_agent_max_workers():
    """Patching drops the SDK's agent fan-out from 20 to the configured cap."""
    patch_evals_sdk()
    from agentplatform._genai import _evals_common as ec

    assert ec.AGENT_MAX_WORKERS == _sdk_patches._AGENT_MAX_WORKERS
    assert ec.AGENT_MAX_WORKERS < 20


def test_patched_agent_run_retries_empty_turn():
    """The wrapped _execute_agent_run_with_retry retries an empty turn."""
    _sdk_patches._PATCHED = False
    patch_evals_sdk()

    # Replace the (already-wrapped) function's target by re-wrapping a fake orig.
    seq = [[], [{"content": {"parts": [{"text": "recovered"}]}}]]
    state = {"n": 0}

    def fake_orig(row, contents, agent_engine, max_retries=3):
        r = seq[state["n"]]
        state["n"] += 1
        return r

    # Rebuild the wrapper around our fake orig, mirroring _patch_retry_on_empty.
    def wrapped(row, contents, agent_engine, max_retries=3):
        return _run_with_empty_retry(
            lambda: fake_orig(row, contents, agent_engine, max_retries=max_retries),
            retries=_sdk_patches._EMPTY_RETRIES,
            sleep_fn=lambda a: None,
        )

    out = wrapped(row=None, contents=None, agent_engine=None)
    assert out == [{"content": {"parts": [{"text": "recovered"}]}}]
    assert state["n"] == 2


def test_warm_agent_engine_counts_content_and_swallows_errors():
    """Warmup counts queries that returned content and never raises."""

    class FakeEngine:
        def stream_query(self, user_id, message):
            # First call yields content, second yields nothing.
            if user_id.endswith("0"):
                yield {"content": {"parts": [{"text": "pong"}]}}
            else:
                return
            return

    assert warm_agent_engine(FakeEngine(), n=2) == 1

    class BoomEngine:
        def stream_query(self, user_id, message):
            raise RuntimeError("cold")
            yield  # pragma: no cover

    assert warm_agent_engine(BoomEngine(), n=2) == 0


def test_flip_extra_allows_error_field_on_result():
    """EvaluationItemResult loads candidate results carrying an error object."""
    patch_evals_sdk()
    from agentplatform._genai import types as t

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
