"""Tests for restoring the tool-call ids ADK strips from a wrapped LiteLlm.

ADK decides whether to keep its synthetic ``adk-*`` function-call ids by
``isinstance(agent.canonical_model, LiteLlm)`` (``flows/llm_flows/contents.py``).
Our Claude backbones are wrapped — ``TierRoutingLlm`` -> ``RetryingLlm`` ->
``LiteLlm`` — so that check is False and the ids are stripped, even though the
provider underneath pairs tool calls with their results *by id*. See
docs/notes/router-empty-stream-retry.md.
"""

from __future__ import annotations

from types import SimpleNamespace

from google.genai import types

from src.models.tool_call_ids import restore_tool_call_ids


def _call(name, id=None):
    return types.Part(function_call=types.FunctionCall(name=name, args={}, id=id))


def _resp(name, id=None):
    return types.Part(
        function_response=types.FunctionResponse(name=name, response={"ok": True}, id=id)
    )


def _req(*contents):
    return SimpleNamespace(contents=list(contents))


def _content(*parts, role="model"):
    return types.Content(role=role, parts=list(parts))


class TestRestoreToolCallIds:
    def test_pairs_a_call_with_its_response(self):
        req = _req(
            _content(_call("search_flights")), _content(_resp("search_flights"), role="user")
        )

        assert restore_tool_call_ids(req) == 2

        call_id = req.contents[0].parts[0].function_call.id
        assert call_id
        assert req.contents[1].parts[0].function_response.id == call_id

    def test_distinct_calls_get_distinct_ids(self):
        req = _req(
            _content(_call("search_flights"), _call("search_hotels")),
            _content(_resp("search_flights"), _resp("search_hotels"), role="user"),
        )

        restore_tool_call_ids(req)

        calls = [p.function_call.id for p in req.contents[0].parts]
        resps = [p.function_response.id for p in req.contents[1].parts]
        assert len(set(calls)) == 2
        assert resps == calls

    def test_repeated_calls_to_one_tool_pair_in_order(self):
        """Two ``get_expenses`` hops must not collapse onto a single id."""
        req = _req(
            _content(_call("get_expenses")),
            _content(_resp("get_expenses"), role="user"),
            _content(_call("get_expenses")),
            _content(_resp("get_expenses"), role="user"),
        )

        restore_tool_call_ids(req)

        first, second = req.contents[0].parts[0], req.contents[2].parts[0]
        assert first.function_call.id != second.function_call.id
        assert req.contents[1].parts[0].function_response.id == first.function_call.id
        assert req.contents[3].parts[0].function_response.id == second.function_call.id

    def test_existing_ids_are_left_alone(self):
        """A provider that supplied real ids must keep them verbatim."""
        req = _req(_content(_call("f", id="toolu_real")), _content(_resp("f", id="toolu_real")))

        assert restore_tool_call_ids(req) == 0
        assert req.contents[0].parts[0].function_call.id == "toolu_real"

    def test_is_idempotent(self):
        req = _req(_content(_call("f")), _content(_resp("f"), role="user"))

        restore_tool_call_ids(req)
        first = req.contents[0].parts[0].function_call.id
        assert restore_tool_call_ids(req) == 0
        assert req.contents[0].parts[0].function_call.id == first

    def test_an_orphan_response_still_gets_an_id(self):
        """Better a synthetic id than a KeyError that empties the whole stream."""
        req = _req(_content(_resp("f"), role="user"))

        assert restore_tool_call_ids(req) == 1
        assert req.contents[0].parts[0].function_response.id

    def test_text_only_contents_are_untouched(self):
        req = _req(_content(types.Part(text="hello"), role="user"))
        assert restore_tool_call_ids(req) == 0

    def test_tolerates_missing_contents_and_parts(self):
        assert restore_tool_call_ids(SimpleNamespace(contents=None)) == 0
        assert restore_tool_call_ids(SimpleNamespace()) == 0
        assert restore_tool_call_ids(_req(types.Content(role="user", parts=None))) == 0

    def test_replaces_the_part_field_rather_than_mutating_the_shared_object(self):
        """ADK shallow-copies parts; the nested ``FunctionCall`` is still shared
        with the session event, so mutating it in place corrupts history."""
        shared = types.FunctionCall(name="f", args={}, id=None)
        part = types.Part(function_call=shared)
        restore_tool_call_ids(_req(_content(part)))

        assert part.function_call.id  # the part now points at a fixed copy
        assert shared.id is None  # the session's object is untouched
