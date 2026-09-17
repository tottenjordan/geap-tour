"""`simulated_eval`'s two silent-failure guards.

Both were found on 2026-09-09 by running it against a live engine on
google-cloud-aiplatform 2.1.0. The eval run reported **SUCCEEDED**, the harness
printed "(no metrics returned)", and the result file said **`all_passed: true`** —
a green verdict over a conversation that had been destroyed during parsing.
"""

from __future__ import annotations

import pytest


class TestExtraFieldsArePreservedNotDropped:
    """`extra='allow'`, not `'ignore'` — the one word that was the whole bug.

    The API returns turn data carrying ADK Event fields (`content`, `author`,
    `actions`, `invocation_id`, `id`, `timestamp`). `ConversationTurn` is
    `extra='forbid'`, so parsing raises. The patch stopped the exception with
    `extra='ignore'`, which **discards every unrecognised field** — and on this SDK
    those fields *are* the conversation. Measured:

        extra='ignore'  ->  1 turn,  all fields dropped, events=[]  -> metrics {}
        extra='allow'   ->  3 turns, content/author/actions preserved
    """

    def test_the_patch_allows_rather_than_ignores(self):
        from agentplatform._genai.types import evals as et

        from src.eval.simulated_eval import _patch_evals_extra_fields

        _patch_evals_extra_fields()
        for cls in (et.ConversationTurn, et.AgentData):
            assert cls.model_config["extra"] == "allow", (
                f"{cls.__name__} is '{cls.model_config['extra']}' — 'ignore' silently "
                "drops the conversation payload and the run scores nothing"
            )

    def test_a_turn_carrying_event_fields_keeps_them(self):
        """The behavioural contract, on the real SDK class: parse a turn shaped like
        what the API actually sends and confirm the payload survives."""
        from agentplatform._genai.types import evals as et

        from src.eval.simulated_eval import _patch_evals_extra_fields

        _patch_evals_extra_fields()
        turn = et.ConversationTurn.model_validate(
            {
                "turn_id": "t1",
                "author": "coordinator_agent",
                "content": {"parts": [{"text": "Booked FL001."}]},
                "invocation_id": "e-123",
            }
        )
        dumped = turn.model_dump()
        assert dumped.get("author") == "coordinator_agent", (
            "the author field was dropped — extra='ignore' regressed"
        )
        assert dumped.get("content"), "the conversation content was dropped"

    def test_forbid_would_still_raise(self):
        """Guard the guard: confirm the base class really is strict, so this patch
        is doing something. If the SDK relaxes it, the patch becomes removable."""
        from agentplatform._genai.types import evals as et

        original = et.ConversationTurn.model_config.get("extra")
        try:
            et.ConversationTurn.model_config["extra"] = "forbid"
            et.ConversationTurn.__pydantic_complete__ = False
            et.ConversationTurn.model_rebuild(force=True)
            with pytest.raises(Exception, match=r"[Ee]xtra"):
                et.ConversationTurn.model_validate({"turn_id": "t", "author": "x"})
        finally:
            et.ConversationTurn.model_config["extra"] = original
            et.ConversationTurn.__pydantic_complete__ = False
            et.ConversationTurn.model_rebuild(force=True)


class TestTheParentModelAcceptsTheRelaxedChild:
    """The patch must relax EVERY config before it rebuilds ANY schema.

    A pydantic-v2 parent compiles its children's schemas into its own. The patch did
    `set config; rebuild` in one loop over `dir()` — which is alphabetical — so
    `AgentData` was rebuilt BEFORE `ConversationTurn` was relaxed, freezing the old
    `forbid` child into the parent. Rebuilding the child afterwards does not propagate
    upward.

    `run_inference` then died with `49 validation errors for AgentData` on
    `turns.N.author`, `turns.N.invocation_id`, … and `simulated_eval` returned zero
    metrics for a week.

    **Why the existing tests missed it, which is the point.** One asserts
    `model_config['extra'] == 'allow'` — and both classes reported exactly that. The
    other validates a `ConversationTurn` directly — and the child was genuinely fixed.
    The parent was never exercised, and the parent is what `run_inference` constructs.

        AgentData.model_config['extra']        -> 'allow'
        ConversationTurn.model_config['extra'] -> 'allow'
        AgentData.model_validate({'turns': [<Event-shaped turn>]}) -> ValidationError

    Config is not behaviour.
    """

    def test_agent_data_accepts_a_turn_carrying_event_fields(self):
        """THE regression. Reverting to a single set-and-rebuild loop turns this red
        while every config assertion above stays green."""
        import pydantic
        from agentplatform._genai.types import evals as et

        from src.eval.simulated_eval import _patch_evals_extra_fields

        _patch_evals_extra_fields()
        try:
            data = et.AgentData.model_validate(
                {
                    "turns": [
                        {
                            "author": "coordinator_agent",
                            "content": {"parts": [{"text": "Booked FL001."}]},
                            "invocation_id": "e-123",
                            "id": "evt-1",
                            "timestamp": 1789644498.2,
                        }
                    ]
                }
            )
        except pydantic.ValidationError as exc:
            raise AssertionError(
                "AgentData rejected an Event-shaped turn even though its own config "
                f"reads '{et.AgentData.model_config.get('extra')}'. The parent froze a "
                f"stale child schema — relax all configs BEFORE rebuilding. {exc}"
            ) from exc

        assert data.turns, "the turn survived parsing but the list is empty"

    def test_the_patch_relaxes_before_it_rebuilds(self):
        """Structural guard on the ordering itself, so the two-phase shape survives a
        refactor that keeps the tests passing by accident."""
        import inspect

        from src.eval import simulated_eval

        src = inspect.getsource(simulated_eval._patch_evals_extra_fields)
        relax = src.index('cls.model_config["extra"] = "allow"')
        rebuild = src.index("model_rebuild(force=True)")
        assert relax < rebuild, "a rebuild happens before the relax pass completes"
        # and they must be in SEPARATE passes over the same collection, not one loop
        assert src.count("for cls in targets:") == 2, (
            "the relax and rebuild passes were merged back into one loop — a parent "
            "then compiles a child that has not been relaxed yet"
        )


class TestAnEmptyRunIsNotAPass:
    """Zero metrics used to report `all_passed: true`.

    `all_pass` starts True and only flips on a *failing* metric, so a run that
    scored nothing sailed through. That is how the data loss above stayed
    invisible: the harness said pass, the eval run said SUCCEEDED, and the only
    hint was one parenthetical line of output.
    """

    def test_no_metrics_means_failure(self):
        import inspect

        from src.eval import simulated_eval

        src = inspect.getsource(simulated_eval.run_simulated_eval)
        assert "if not metric_results:" in src, "the empty-result branch was renamed"
        empty_branch = src.split("if not metric_results:", 1)[1]
        assert "all_pass = False" in empty_branch.split("return", 1)[0], (
            "an empty run must set all_pass=False — otherwise scoring nothing reports as success"
        )


class TestFlatEventsAreRegroupedIntoTurns:
    """`ConversationTurn` is `{turn_index, turn_id, events[]}`, but aiplatform 2.1.0
    returns each ADK Event as a top-level entry in `turns` with `events` empty.

    The multi-turn raters read `turn.events`. Finding nothing, they return **no
    metrics at all** while the evaluation run still reports SUCCEEDED — measured
    live on 2026-09-09.
    """

    @staticmethod
    def _ev(inv, text):
        return {
            "invocation_id": inv,
            "author": "coordinator_agent",
            "content": {"parts": [{"text": text}]},
        }

    def test_events_sharing_an_invocation_become_one_turn(self):
        from src.eval.simulated_eval import regroup_events_into_turns

        out = regroup_events_into_turns(
            {
                "turns": [
                    self._ev("i1", "a"),
                    self._ev("i1", "b"),
                    self._ev("i2", "c"),
                ]
            }
        )
        turns = out["turns"]
        assert [t["turn_index"] for t in turns] == [0, 1]
        assert [len(t["events"]) for t in turns] == [2, 1]

    def test_order_is_preserved(self):
        from src.eval.simulated_eval import regroup_events_into_turns

        out = regroup_events_into_turns(
            {
                "turns": [
                    self._ev("i1", "first"),
                    self._ev("i2", "second"),
                    self._ev("i3", "third"),
                ]
            }
        )
        texts = [t["events"][0]["content"]["parts"][0]["text"] for t in out["turns"]]
        assert texts == ["first", "second", "third"]

    def test_already_correct_data_is_untouched(self):
        """The moment the SDK returns the declared shape this must become a no-op —
        otherwise the workaround starts corrupting correct data."""
        from src.eval.simulated_eval import regroup_events_into_turns

        good = {"turns": [{"turn_index": 0, "turn_id": "t", "events": [self._ev("i1", "x")]}]}
        assert regroup_events_into_turns(good) is good

    def test_events_without_an_invocation_id_are_not_merged(self):
        """A wrong grouping yields plausible-looking multi-turn scores, which is
        worse than a conservative one. No id -> its own turn."""
        from src.eval.simulated_eval import regroup_events_into_turns

        out = regroup_events_into_turns(
            {
                "turns": [
                    self._ev(None, "a"),
                    self._ev(None, "b"),
                ]
            }
        )
        assert len(out["turns"]) == 2

    def test_degenerate_inputs_pass_through(self):
        from src.eval.simulated_eval import regroup_events_into_turns

        assert regroup_events_into_turns(None) is None
        assert regroup_events_into_turns({}) == {}
        assert regroup_events_into_turns({"turns": []}) == {"turns": []}

    def test_the_dataset_wrapper_rewrites_every_row(self):
        import pandas as pd

        from src.eval.simulated_eval import regroup_dataset_turns

        class R:
            eval_dataset_df = pd.DataFrame(
                {
                    "agent_data": [{"turns": [TestFlatEventsAreRegroupedIntoTurns._ev("i1", "x")]}]
                    * 2
                }
            )

        out = regroup_dataset_turns(R())
        for cell in out.eval_dataset_df["agent_data"]:
            assert cell["turns"][0]["events"], "row was not regrouped"


class TestTheAiplatformPinIsDeliberate:
    """`google-cloud-aiplatform` is pinned to 2.1.0, not floored.

    2.1.3 reproducibly breaks this very module: the evaluation run returns FAILED with
    `code=13 'Result item initialization failed due to an internal error.'` Measured
    2026-09-17 with identical code and the same engine on both sides — 2/2 failures on
    2.1.3, success with metrics on 2.1.0, and success again on 2.1.0 with the other 47
    package upgrades in place. The upgrade is the sole variable.

    The pin looks like staleness and will attract a "why are we behind?" cleanup, so
    the reason lives next to it and this test keeps them together. Relaxing it to a
    floor silently re-breaks simulated_eval — and the full unit suite stays green,
    because the failure is server-side in the eval run.
    """

    @staticmethod
    def _pyproject() -> str:
        import pathlib as _p

        return _p.Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text()

    def test_it_is_an_exact_pin_not_a_floor(self) -> None:
        text = self._pyproject()
        assert 'google-cloud-aiplatform[adk,agent-engines,evaluation]==2.1.0"' in text, (
            "aiplatform is no longer pinned to ==2.1.0; 2.1.3 breaks simulated_eval's "
            "evaluation run with code=13 'Result item initialization failed'"
        )

    def test_the_reason_is_recorded_beside_the_pin(self) -> None:
        """A pin without a reason gets removed by the next person who sees it."""
        text = self._pyproject()
        head = text[: text.index("google-cloud-aiplatform[adk,agent-engines,evaluation]==2.1.0")]
        window = head[-700:]
        assert "2.1.3" in window, "the pin does not say which version broke"
        assert "simulated_eval" in window, "the pin does not say what it broke"


class TestTheSdkDiscardsTheSimulatorOnTheRuntimePath:
    """Why `--max-turns` does nothing against a deployed engine.

    `agentplatform._genai._evals_common._run_agent` branches on runtime-vs-local and
    passes `user_simulator_config=None` for a deployed engine, forwarding it only for
    an in-process `LlmAgent`. So the multi-turn user simulation never runs on the path
    this repo uses, `max_turn` never leaves the client, and the three `multi_turn_*`
    raters grade a single turn — which is why two of them score exactly 0.00.

    Measured 2026-09-17 on google-cloud-aiplatform 2.1.0: `max_turn` 1, 3 and 8 all
    returned one invocation and zero `user`-authored events, and the raw service
    response stopped after the agent's first reply.

    **This test is a drift detector, not an endorsement.** It asserts the upstream
    limitation still exists. When a future SDK starts honouring the config, this test
    FAILS — which is the notification to delete the warning in `simulated_eval`, drop
    this class, and fold the multi-turn smoke check back into the eval gate's guard.
    Without it the workaround outlives the bug, silently, like the quarantine did.
    """

    @staticmethod
    def _spy_on_execute(monkeypatch):
        from agentplatform._genai import _evals_common as ec

        seen = {}

        def fake_execute(**kwargs):
            seen["user_simulator_config"] = kwargs.get("user_simulator_config")
            seen["inference_fn"] = getattr(kwargs.get("inference_fn"), "__name__", "")
            return []

        monkeypatch.setattr(ec, "_execute_inference_concurrently", fake_execute)
        return ec, seen

    def test_a_deployed_engine_gets_none(self, monkeypatch):
        """THE finding. Not our config — the SDK drops it."""
        import pandas as pd
        from agentplatform import types

        ec, seen = self._spy_on_execute(monkeypatch)
        cfg = types.evals.UserSimulatorConfig(max_turn=5, model_name="gemini-2.5-flash")

        ec._run_agent(
            api_client=object(),
            runtime="projects/p/locations/us-central1/reasoningEngines/1",
            agent=None,
            prompt_dataset=pd.DataFrame({"starting_prompt": ["hi"]}),
            user_simulator_config=cfg,
        )
        assert seen["user_simulator_config"] is None, (
            "upstream now forwards the simulator config for a deployed engine — "
            "multi-turn may work. Re-measure, then remove the single-turn warning in "
            "src/eval/simulated_eval.py and re-arm the eval-gate AND guard."
        )

    def test_a_local_agent_keeps_it(self, monkeypatch):
        """The contrast that proves the branch is the cause, not a missing feature."""
        import pandas as pd
        from agentplatform import types

        ec, seen = self._spy_on_execute(monkeypatch)
        cfg = types.evals.UserSimulatorConfig(max_turn=5, model_name="gemini-2.5-flash")

        ec._run_agent(
            api_client=object(),
            runtime=None,
            agent=object(),  # stands in for an LlmAgent; _run_agent only checks truthiness
            prompt_dataset=pd.DataFrame({"starting_prompt": ["hi"]}),
            user_simulator_config=cfg,
        )
        assert seen["user_simulator_config"] is cfg
        assert "local" in seen["inference_fn"]

    def test_our_config_is_still_well_formed(self, monkeypatch):
        """Guard against 'fixing' this by mangling our side.

        `max_turn` is a real declared field and our value validates cleanly. The bug
        is downstream of us, so the config we send must stay correct — it is what a
        repaired SDK would honour.
        """
        from agentplatform import types

        validated = types.EvalRunInferenceConfig.model_validate(
            {"user_simulator_config": {"max_turn": 3, "model_name": "gemini-2.5-flash"}}
        )
        assert validated.user_simulator_config.max_turn == 3

    def test_the_module_warns_operators(self):
        """A limitation only recorded in a docstring is one nobody hits at runtime."""
        import inspect

        from src.eval import simulated_eval

        src = inspect.getsource(simulated_eval.run_simulated_eval)
        assert "NO EFFECT" in src and "SINGLE-TURN" in src
