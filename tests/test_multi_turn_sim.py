"""A multi-turn eval that is actually multi-turn.

`simulated_eval` cannot be, against a deployed engine: the SDK passes
`user_simulator_config=None` on the `runtime` path, so `--max-turns` never leaves the
client and every conversation is one turn. This module owns the loop instead.

The properties worth defending are the ones whose absence would make the output *look*
like a multi-turn eval while measuring something else:

* the whole conversation runs in ONE session (a fresh session per turn is a pile of
  unrelated single turns wearing a transcript);
* user turns appear in the emitted data (the SDK path produced zero `user`-authored
  events, so the raters graded a monologue);
* an empty engine response stops the loop and is reported as infra, not as a short
  conversation.

Every test here runs offline — the loop's I/O is injectable precisely so this is
possible.
"""

from __future__ import annotations

import pytest

from src.eval import multi_turn_sim as mts


def _agent_events(text: str, *, invocation: str = "inv-1", tool: str | None = None) -> list[dict]:
    parts: list[dict] = []
    if tool:
        parts.append({"function_call": {"name": tool, "args": {}}})
    parts.append({"text": text})
    return [
        {
            "content": {"parts": parts, "role": "model"},
            "author": "coordinator_agent",
            "invocation_id": invocation,
        }
    ]


class _FakeEngine:
    """Records sessions and messages; replies from a scripted list."""

    def __init__(self, replies: list[list[dict]]) -> None:
        self.replies = replies
        self.sessions_created = 0
        self.messages: list[str] = []
        self.session_ids: list[str] = []

    def create_session(self, resource_name, user_id, *, token=None):
        self.sessions_created += 1
        return f"session-{self.sessions_created}"

    def stream(self, resource_name, *, message, user_id, session_id, token=None):
        self.messages.append(message)
        self.session_ids.append(session_id)
        i = len(self.messages) - 1
        return self.replies[i] if i < len(self.replies) else _agent_events("ok")


def _run(engine: _FakeEngine, simulator_replies: list[str], **kw):
    it = iter(simulator_replies)
    return mts.simulate_conversation(
        "projects/p/locations/l/reasoningEngines/1",
        kw.pop("starting_prompt", "Book me a flight to JFK."),
        kw.pop("plan", "1. Ask to cancel. 2. Confirm."),
        simulate_fn=lambda _prompt: next(it, mts.END_SENTINEL),
        create_session_fn=engine.create_session,
        stream_fn=engine.stream,
        **kw,
    )


class TestTheConversationIsActuallyOneConversation:
    def test_one_session_for_the_whole_conversation(self):
        """THE property. Per-turn sessions would give the agent no memory of the
        previous turn, producing a transcript that reads multi-turn while every turn
        is really a cold start — indistinguishable in the output, and wrong."""
        engine = _FakeEngine([_agent_events("a"), _agent_events("b"), _agent_events("c")])
        result = _run(engine, ["second", "third"], max_turns=3)

        assert engine.sessions_created == 1
        assert len(set(engine.session_ids)) == 1
        assert result["turn_count"] == 3

    def test_the_simulator_drives_subsequent_turns(self):
        engine = _FakeEngine([_agent_events("a"), _agent_events("b")])
        _run(engine, ["what about hotels?"], max_turns=2, starting_prompt="flights please")

        assert engine.messages == ["flights please", "what about hotels?"]

    def test_the_plan_and_transcript_reach_the_simulator(self):
        """A simulator that cannot see the plan improvises, and the run stops testing
        the scenario it claims to test."""
        seen: list[str] = []
        engine = _FakeEngine([_agent_events("hello"), _agent_events("done")])
        mts.simulate_conversation(
            "res",
            "start here",
            "STEP ONE: cancel HTL-5542",
            simulate_fn=lambda p: (seen.append(p), mts.END_SENTINEL)[1],
            create_session_fn=engine.create_session,
            stream_fn=engine.stream,
            max_turns=3,
        )
        assert "STEP ONE: cancel HTL-5542" in seen[0]
        assert "start here" in seen[0]
        assert "hello" in seen[0], "the agent's reply must be in the transcript"


class TestStoppingConditions:
    def test_end_sentinel_stops_the_loop(self):
        engine = _FakeEngine([_agent_events("a"), _agent_events("b")])
        result = _run(engine, [mts.END_SENTINEL], max_turns=5)

        assert result["turn_count"] == 1
        assert result["stopped"] == "plan_complete"

    def test_max_turns_is_respected(self):
        engine = _FakeEngine([_agent_events(str(i)) for i in range(10)])
        result = _run(engine, ["go"] * 10, max_turns=3)

        assert result["turn_count"] == 3
        assert result["stopped"] == "max_turns"

    def test_an_empty_response_stops_and_is_labelled_infra(self):
        """An engine returning zero characters is an infra failure. Driving a
        simulator against silence yields a transcript that scores as bad *quality* —
        the exact conflation `online_monitor`'s infra_empty_rate exists to prevent."""
        engine = _FakeEngine([_agent_events("fine"), [], _agent_events("never reached")])
        result = _run(engine, ["two", "three"], max_turns=4)

        assert result["stopped"] == "empty_response"
        assert result["turn_count"] == 2, "must not keep driving after an empty turn"

    def test_the_simulator_never_sees_the_dead_turn(self):
        calls = []
        engine = _FakeEngine([[]])
        mts.simulate_conversation(
            "res",
            "hi",
            "plan",
            simulate_fn=lambda p: (calls.append(p), "next")[1],
            create_session_fn=engine.create_session,
            stream_fn=engine.stream,
            max_turns=3,
        )
        assert calls == [], "no simulator spend on a conversation the engine dropped"


class TestTheEmittedShapeIsWhatTheRatersRead:
    def test_each_turn_carries_the_user_message(self):
        """The gap that made the SDK path unscorable: a captured raw response had
        ZERO user-authored events, so the raters graded a monologue."""
        turn = mts.build_turn(0, "cancel my hotel", _agent_events("which booking?"))
        authors = [e.get("author") for e in turn["events"]]

        assert "user" in authors
        assert turn["events"][0]["content"]["parts"][0]["text"] == "cancel my hotel"
        assert turn["events"][0]["content"]["role"] == "user"

    def test_turn_id_borrows_the_invocation_id(self):
        turn = mts.build_turn(2, "hi", _agent_events("hey", invocation="inv-xyz"))
        assert turn["turn_id"] == "inv-xyz"
        assert turn["turn_index"] == 2

    def test_turn_id_falls_back_when_the_engine_sends_none(self):
        turn = mts.build_turn(1, "hi", [{"content": {"parts": [{"text": "hey"}]}}])
        assert turn["turn_id"] == "turn-1"

    def test_the_dataframe_has_the_columns_the_raters_expect(self):
        engine = _FakeEngine([_agent_events("a"), _agent_events("b")])
        convo = _run(engine, ["more"], max_turns=2)
        df = mts.conversations_to_dataframe(
            [{"starting_prompt": "p", "conversation_plan": "c"}], [convo]
        )

        assert list(df.columns) == ["starting_prompt", "conversation_plan", "agent_data"]
        turns = df["agent_data"].iloc[0]["turns"]
        assert len(turns) == 2
        assert [t["turn_index"] for t in turns] == [0, 1]


class TestParsingTheSimulator:
    @pytest.mark.parametrize("raw", ["[END]", "END", "[end].", "  [END]  ", "", "   ", None])
    def test_finished_markers_end_the_conversation(self, raw):
        assert mts.parse_simulator_reply(raw) is None

    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            ("Book the second flight.", "Book the second flight."),
            ('"Book the second flight."', "Book the second flight."),
            ("USER: Book it.", "Book it."),
            ("  padded  ", "padded"),
        ],
    )
    def test_real_utterances_survive(self, raw, want):
        assert mts.parse_simulator_reply(raw) == want

    def test_a_message_containing_the_word_end_is_not_a_sentinel(self):
        """`"End the booking"` is a user instruction, not a stop signal. Matching on
        a substring here would silently truncate legitimate conversations."""
        assert mts.parse_simulator_reply("End the booking please") == "End the booking please"


class TestTheSummaryTellsTheTruth:
    def test_it_counts_genuinely_multi_turn_conversations(self):
        convos = [
            {"turn_count": 3, "stopped": "plan_complete", "tool_calls": ["a"]},
            {"turn_count": 1, "stopped": "plan_complete", "tool_calls": []},
        ]
        s = mts.summarize(convos)
        assert s["multi_turn_conversations"] == 1
        assert s["turns_mean"] == 2.0
        assert s["tool_calls"] == ["a"]

    def test_empty_responses_are_surfaced_not_averaged_away(self):
        convos = [
            {"turn_count": 1, "stopped": "empty_response", "tool_calls": []},
            {"turn_count": 3, "stopped": "max_turns", "tool_calls": []},
        ]
        s = mts.summarize(convos)
        assert s["empty_response_conversations"] == 1
        assert "EMPTY response" in mts.render(s)

    def test_tool_calls_are_collected_from_the_stream(self):
        engine = _FakeEngine([_agent_events("searching", tool="search_mcp_search_flights")])
        result = _run(engine, [mts.END_SENTINEL], max_turns=1)
        assert result["tool_calls"] == ["search_mcp_search_flights"]


class TestTheCliDoesNotSpendByAccident:
    def test_dry_run_contacts_nothing(self, capsys):
        assert mts.main(["--agent-id", "123", "--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "engine calls" in out, "a dry run must state what it would cost"

    def test_dry_run_cost_tracks_the_arguments(self, capsys):
        mts.main(["--agent-id", "1", "--dry-run", "--scenario-count", "3", "--max-turns", "5"])
        out = capsys.readouterr().out
        assert "~15 engine calls" in out
        assert "~12 simulator calls" in out


class TestEmptyStreamsAreNotScoredAsQuality:
    """A dead stream has nothing for a rubric to grade — and the raters grade it.

    This module labelled `stopped=empty_response` correctly from day one and then
    handed those conversations to the scorer anyway, so one empty stream dragged a
    run's mean down and an INFRA failure was published as QUALITY. The exact
    conflation `multi_agent_batch_eval.partition_empty_responses` prevents.

    Not caught by review — caught by a discrimination run whose baseline came back
    0.25/1.00/0.17 against 1.00/1.00/1.00 the day before. Worse, it made
    `trajectory_quality` read BLIND to a defect it actually catches at -0.83: an
    already-floored metric cannot fall further, so infra contamination looks
    exactly like rubric failure.
    """

    def test_empty_conversations_are_dropped_with_their_scenarios(self):
        """Scenarios and conversations are positional; dropping one without the
        other silently mislabels every remaining row."""
        scenarios = [{"starting_prompt": "a"}, {"starting_prompt": "b"}, {"starting_prompt": "c"}]
        convos = [
            {"stopped": "plan_complete"},
            {"stopped": "empty_response"},
            {"stopped": "max_turns"},
        ]
        sc, cv, n_empty = mts.partition_empty_conversations(scenarios, convos)

        assert n_empty == 1
        assert [s["starting_prompt"] for s in sc] == ["a", "c"]
        assert [c["stopped"] for c in cv] == ["plan_complete", "max_turns"]

    def test_a_healthy_run_is_untouched(self):
        scenarios = [{"starting_prompt": "a"}]
        convos = [{"stopped": "plan_complete"}]
        assert mts.partition_empty_conversations(scenarios, convos) == (scenarios, convos, 0)

    def test_an_all_empty_run_leaves_nothing_to_score(self):
        """The caller must report this as infra, not publish a mean over zero items,
        which would render as catastrophic quality."""
        convos = [{"stopped": "empty_response"}] * 2
        sc, cv, n_empty = mts.partition_empty_conversations([{}, {}], convos)
        assert (sc, cv, n_empty) == ([], [], 2)

    def test_only_empty_response_is_dropped(self):
        """`max_turns` and `plan_complete` are normal endings; dropping them would
        silently shrink every run."""
        convos = [{"stopped": s} for s in ("plan_complete", "max_turns", "empty_response")]
        _, cv, _ = mts.partition_empty_conversations([{}] * 3, convos)
        assert {c["stopped"] for c in cv} == {"plan_complete", "max_turns"}
