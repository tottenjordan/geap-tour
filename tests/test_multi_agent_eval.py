"""Tests for multi-agent eval infrastructure — validates configs, eval cases, and evalset files."""

import json
from pathlib import Path

import pytest

EVALSETS_DIR = Path(__file__).parent.parent / "src" / "eval" / "evalsets"
SCENARIOS_DIR = Path(__file__).parent.parent / "src" / "eval" / "scenarios"

EVALSET_FILES = [
    "coordinator.evalset.json",
    "travel_agent.evalset.json",
    "expense_agent.evalset.json",
    "router_agent.evalset.json",
]

SCENARIO_FILES = [
    "coordinator_scenarios.json",
    "travel_scenarios.json",
    "expense_scenarios.json",
    "router_scenarios.json",
]


class TestAgentInfoBuilder:
    def test_coordinator_agent_info(self):
        from src.eval.agent_eval_configs import build_agent_info

        info = build_agent_info("coordinator_agent")
        assert info is not None

    def test_travel_agent_info(self):
        from src.eval.agent_eval_configs import build_agent_info

        info = build_agent_info("travel_agent")
        assert info is not None

    def test_expense_agent_info(self):
        from src.eval.agent_eval_configs import build_agent_info

        info = build_agent_info("expense_agent")
        assert info is not None

    def test_router_agent_info(self):
        from src.eval.agent_eval_configs import build_agent_info

        info = build_agent_info("router_agent")
        assert info is not None

    def test_unknown_agent_raises(self):
        from src.eval.agent_eval_configs import build_agent_info

        with pytest.raises(ValueError):
            build_agent_info("nonexistent_agent")


class TestCoordinatorDescriptorMatchesTheAgent:
    """The eval descriptors must describe the coordinator that is actually deployed.

    The coordinator holds its MCP toolsets directly and no longer carries the
    travel_agent/expense_agent AgentTools (0 measured delegations — see
    docs/notes/coordinator-router-learnings.md). These descriptors feed the
    delegation-aware ``geap_tool_use`` judge, so a stale multi-agent topology
    would have it grading routing behaviour the agent cannot exhibit.
    """

    def _coordinator_infos(self):
        from src.eval.agent_eval_configs import build_agent_info
        from src.eval.batch_eval import _build_agent_info

        return [build_agent_info("coordinator_agent"), _build_agent_info()]

    def test_descriptors_declare_no_sub_agents(self):
        for info in self._coordinator_infos():
            assert info.agents["coordinator_agent"].sub_agents == []

    def test_descriptors_describe_only_the_coordinator(self):
        for info in self._coordinator_infos():
            assert list(info.agents) == ["coordinator_agent"]

    def test_descriptors_name_no_specialist(self):
        for info in self._coordinator_infos():
            config = info.agents["coordinator_agent"]
            text = f"{config.description} {config.instruction}"
            assert "travel_agent" not in text
            assert "expense_agent" not in text


class TestRouterDescriptorMatchesTheAgent:
    """The router stopped delegating on 2026-08-20 — the descriptor must follow.

    ``src/router/agents.py`` is now ONE direct-tools agent that swaps its model
    per tier via ``TierRoutingLlm``; the five tier agents are no longer reachable
    from the root agent and ``transfer_to_agent`` is gone. A descriptor still
    claiming ``sub_agents=[lite_agent, …]`` tells the eval judge about an
    architecture that cannot run, and that stale ``transfer_to_agent`` was also
    the thing that used to guarantee a ``function_call`` in every trace (see
    docs/notes/router-tool-use-quality.md).
    """

    def _router_info(self):
        from src.eval.agent_eval_configs import build_agent_info

        return build_agent_info("router_agent")

    def test_descriptor_declares_no_sub_agents(self):
        assert self._router_info().agents["router_agent"].sub_agents == []

    def test_descriptor_describes_only_the_router(self):
        assert list(self._router_info().agents) == ["router_agent"]

    def test_descriptor_does_not_promise_delegation(self):
        config = self._router_info().agents["router_agent"]
        text = f"{config.description} {config.instruction}".lower()
        assert "delegate" not in text
        assert "transfer_to_agent" not in text
        for tier in ("lite_agent", "flash_agent", "pro_agent", "sonnet_agent", "opus_agent"):
            assert tier not in text


class TestAgentConfigsDeclareTools:
    """``AgentConfig.tools`` is a real field and was populated nowhere.

    The eval service is handed ``AgentData.agents`` built from these descriptors,
    so leaving ``tools`` empty means the tool-use judge is never told what the
    agent can call. Declaring them is what lets it grade the trajectory against
    the real inventory instead of inferring one.
    """

    def test_every_descriptor_declares_tools(self):
        from src.eval.agent_eval_configs import ALL_AGENTS, build_agent_info

        missing = [
            f"{name}/{agent_id}"
            for name in ALL_AGENTS
            for agent_id, config in build_agent_info(name).agents.items()
            if not config.tools
        ]
        assert not missing, f"declare AgentConfig.tools for: {missing}"

    def test_batch_eval_coordinator_descriptor_declares_tools(self):
        """The second coordinator descriptor must not drift from the first."""
        from src.eval.batch_eval import _build_agent_info

        assert _build_agent_info().agents["coordinator_agent"].tools

    @staticmethod
    def _declared_names(config) -> set[str]:
        """Function-declaration names out of ``AgentConfig.tools``.

        ``tools`` is ``list[google.genai.types.Tool]``, not a list of strings —
        each Tool carries ``function_declarations``.
        """
        return {
            decl.name
            for tool in (config.tools or [])
            for decl in (tool.function_declarations or [])
        }

    def test_declared_tools_are_real_mcp_tool_names(self):
        """Guard against inventing names: every declared tool must be expected
        by at least one eval case (``expected_tool``)."""
        from src.eval.agent_eval_configs import _EVAL_CASES, ALL_AGENTS, build_agent_info

        known = {
            case["expected_tool"]
            for cases in _EVAL_CASES.values()
            for case in cases
            if case.get("expected_tool") and case["expected_tool"] != "multiple"
        }
        for name in ALL_AGENTS:
            for agent_id, config in build_agent_info(name).agents.items():
                unknown = self._declared_names(config) - known
                assert not unknown, f"{name}/{agent_id} declares unknown tools: {unknown}"

    def test_every_declaration_carries_a_description(self):
        """A bare name tells the judge nothing about what the tool does."""
        from src.eval.agent_eval_configs import ALL_AGENTS, build_agent_info

        for name in ALL_AGENTS:
            for agent_id, config in build_agent_info(name).agents.items():
                for tool in config.tools or []:
                    for decl in tool.function_declarations or []:
                        assert decl.description, (
                            f"{name}/{agent_id}: {decl.name} has no description"
                        )


class TestPerAgentEngineRouting:
    """The router and the coordinator are separate deployments.

    ``run_multi_agent_batch_eval`` resolved ONE engine for the whole run, so a
    bare invocation (and ``run_all_evals``, which passes no ``--agent-id``) scored
    ROUTER_EVAL_CASES against ``AGENT_ENGINE_ID`` — a coordinator engine — with no
    indication it had done so.
    """

    def test_router_defaults_to_the_router_engine(self):
        from src.config import ROUTER_ENGINE_ID
        from src.eval.multi_agent_batch_eval import _engine_for_agent

        assert _engine_for_agent("router_agent", None).endswith(f"/{ROUTER_ENGINE_ID}")

    def test_coordinator_defaults_to_the_coordinator_engine(self):
        from src.config import AGENT_ENGINE_ID
        from src.eval.multi_agent_batch_eval import _engine_for_agent

        assert _engine_for_agent("coordinator_agent", None).endswith(f"/{AGENT_ENGINE_ID}")

    def test_explicit_agent_id_overrides_the_map(self):
        """The bake-off pins one engine for every agent — that must keep working."""
        from src.eval.multi_agent_batch_eval import _engine_for_agent

        for agent in ("router_agent", "coordinator_agent", "travel_agent"):
            assert _engine_for_agent(agent, "999").endswith("/999")

    def test_full_resource_names_pass_through(self):
        from src.eval.multi_agent_batch_eval import _engine_for_agent

        arn = "projects/p/locations/us-central1/reasoningEngines/123"
        assert _engine_for_agent("router_agent", arn) == arn


class TestToolCallPreflight:
    """``tool_use_quality_v1`` needs at least one tool event in the whole run.

    It is scored from the ``AgentData`` events, not the response text. When every
    item is tool-free the service cannot compute it at all and errors, and the
    harness used to silently report five metrics instead of six with no
    explanation. Reproduced deterministically with ``--limit 1``, whose single
    case ("Find flights from SFO to JFK") makes the router ask "When would you
    like to travel?" rather than search. See docs/notes/router-tool-use-quality.md.
    """

    @staticmethod
    def _agent_data(*part_dicts):
        return {"turns": [{"turn_index": 0, "events": [{"content": {"parts": list(part_dicts)}}]}]}

    def _df(self, *rows):
        import pandas as pd

        return pd.DataFrame({"agent_data": list(rows)})

    def test_counts_items_that_made_tool_calls(self):
        from src.eval.multi_agent_batch_eval import count_tool_call_items

        df = self._df(
            self._agent_data({"function_call": {"name": "search_mcp_search_flights"}}),
            self._agent_data({"text": "When would you like to travel?"}),
        )
        assert count_tool_call_items(df) == (1, 2)

    def test_transfers_count_as_tool_calls(self):
        """The metric counts any function_call — including a delegation."""
        from src.eval.multi_agent_batch_eval import count_tool_call_items

        df = self._df(self._agent_data({"function_call": {"name": "transfer_to_agent"}}))
        assert count_tool_call_items(df) == (1, 1)

    def test_function_response_alone_counts(self):
        from src.eval.multi_agent_batch_eval import count_tool_call_items

        df = self._df(self._agent_data({"function_response": {"name": "search_mcp_search_hotels"}}))
        assert count_tool_call_items(df) == (1, 1)

    def test_json_encoded_agent_data_is_parsed(self):
        """The patched parser stores a JSON string on the error path."""
        from src.eval.multi_agent_batch_eval import count_tool_call_items

        df = self._df(
            json.dumps(self._agent_data({"function_call": {"name": "expense_mcp_submit_expense"}})),
            json.dumps({"error": "Failed to parse agent run response"}),
        )
        assert count_tool_call_items(df) == (1, 2)

    def test_missing_column_or_empty_frame_is_zero(self):
        import pandas as pd

        from src.eval.multi_agent_batch_eval import count_tool_call_items

        assert count_tool_call_items(pd.DataFrame({"response": ["hi"]})) == (0, 1)
        assert count_tool_call_items(None) == (0, 0)

    def test_metric_dropped_only_when_nothing_called_a_tool(self):
        from agentplatform import types

        from src.eval.multi_agent_batch_eval import drop_tool_use_metric_if_unscorable

        metrics = [
            types.RubricMetric.FINAL_RESPONSE_QUALITY,
            types.RubricMetric.TOOL_USE_QUALITY,
        ]
        assert len(drop_tool_use_metric_if_unscorable(metrics, 0, 4)) == 1
        assert len(drop_tool_use_metric_if_unscorable(metrics, 1, 4)) == 2


class TestEvalCasesPerAgent:
    def test_coordinator_has_cases(self):
        from src.eval.agent_eval_configs import get_eval_cases

        cases = get_eval_cases("coordinator_agent")
        assert len(cases) >= 8

    def test_travel_has_cases(self):
        from src.eval.agent_eval_configs import get_eval_cases

        cases = get_eval_cases("travel_agent")
        assert len(cases) >= 8

    def test_expense_has_cases(self):
        from src.eval.agent_eval_configs import get_eval_cases

        cases = get_eval_cases("expense_agent")
        assert len(cases) >= 8

    def test_router_has_cases(self):
        from src.eval.agent_eval_configs import get_eval_cases

        cases = get_eval_cases("router_agent")
        assert len(cases) >= 8

    def test_router_cases_have_expected_complexity(self):
        from src.eval.agent_eval_configs import ROUTER_EVAL_CASES

        for case in ROUTER_EVAL_CASES:
            assert "expected_complexity" in case, (
                f"Missing expected_complexity in: {case.get('prompt', '?')}"
            )
            assert case["expected_complexity"] in ("low", "medium", "high")


class TestCoordinatorDatasetRobustness:
    """The coordinator set is the bake-off dataset — it must be deep enough and
    cover hard multi-step + adversarial cases, not just benign happy paths."""

    def test_coordinator_dataset_is_expanded(self):
        from src.eval.agent_eval_configs import get_eval_cases

        # ~50-case target for a meaningful Gemini-vs-Claude head-to-head.
        assert len(get_eval_cases("coordinator_agent")) >= 45

    def test_coordinator_has_multistep_and_adversarial(self):
        from src.eval.agent_eval_configs import get_eval_cases

        categories = {c["category"] for c in get_eval_cases("coordinator_agent")}
        assert "multi_step" in categories
        assert "adversarial" in categories

    def test_coordinator_cases_have_reference(self):
        # Every coordinator case must carry a non-empty reference so
        # FINAL_RESPONSE_MATCH can score it.
        from src.eval.agent_eval_configs import get_eval_cases

        for case in get_eval_cases("coordinator_agent"):
            assert case.get("reference"), f"Missing reference in: {case.get('prompt', '?')}"


class TestReferenceTrajectories:
    """Reference trajectories (bare tool names) let the deterministic
    trajectory eval score the coordinator's tool-call path. Single-tool cases
    derive theirs from expected_tool; multi-step cases carry a curated order."""

    def test_single_tool_cases_have_reference_trajectory(self):
        from src.eval.agent_eval_configs import get_eval_cases
        from src.eval.batch_eval import _BARE_TOOL

        for case in get_eval_cases("coordinator_agent"):
            if case["expected_tool"] in _BARE_TOOL:
                traj = case.get("reference_trajectory")
                assert traj, f"single-tool case missing reference_trajectory: {case['prompt']}"
                assert traj == [_BARE_TOOL[case["expected_tool"]]]

    def test_reference_trajectories_use_known_bare_tool_names(self):
        from src.eval.agent_eval_configs import get_eval_cases
        from src.eval.batch_eval import KNOWN_BARE_TOOLS

        for case in get_eval_cases("coordinator_agent"):
            for tool in case.get("reference_trajectory") or []:
                assert tool in KNOWN_BARE_TOOLS, f"unknown tool {tool!r} in: {case['prompt']}"

    def test_multi_step_cases_have_ordered_trajectory(self):
        from src.eval.agent_eval_configs import get_eval_cases

        multi = [c for c in get_eval_cases("coordinator_agent") if c["category"] == "multi_step"]
        assert multi, "expected multi_step cases in the coordinator dataset"
        for case in multi:
            traj = case.get("reference_trajectory")
            assert traj and len(traj) >= 2, f"multi_step needs ordered trajectory: {case['prompt']}"


class TestEvalCaseRequiredFields:
    @pytest.fixture(params=["coordinator_agent", "travel_agent", "expense_agent", "router_agent"])
    def agent_cases(self, request):
        from src.eval.agent_eval_configs import get_eval_cases

        return get_eval_cases(request.param)

    def test_prompt_present(self, agent_cases):
        for case in agent_cases:
            assert "prompt" in case, f"Missing 'prompt' field in case: {case}"
            assert len(case["prompt"]) > 0

    def test_category_present(self, agent_cases):
        for case in agent_cases:
            assert "category" in case, f"Missing 'category' field in case: {case}"

    def test_expected_tool_present(self, agent_cases):
        for case in agent_cases:
            assert "expected_tool" in case, f"Missing 'expected_tool' field in case: {case}"


class TestCaseLimit:
    """The CI eval gate uses --limit to cap cases per agent for a fast run."""

    def test_limit_caps_case_count(self):
        from src.eval.multi_agent_batch_eval import _select_cases

        assert len(_select_cases("coordinator_agent", 3)) == 3

    def test_no_limit_returns_all_cases(self):
        from src.eval.agent_eval_configs import get_eval_cases
        from src.eval.multi_agent_batch_eval import _select_cases

        assert _select_cases("coordinator_agent", None) == get_eval_cases("coordinator_agent")

    def test_limit_larger_than_available_returns_all(self):
        from src.eval.agent_eval_configs import get_eval_cases
        from src.eval.multi_agent_batch_eval import _select_cases

        total = len(get_eval_cases("coordinator_agent"))
        assert len(_select_cases("coordinator_agent", total + 100)) == total


class TestLowConfidence:
    """Every metric is flagged low_confidence when graded over too few items."""

    def test_flags_when_below_floor(self):
        from src.eval.multi_agent_batch_eval import _annotate_low_confidence
        from src.eval.stats import MIN_SAMPLES

        results = {"helpfulness": {"score": 0.8}, "safety": {"score": 0.9}}
        _annotate_low_confidence(results, MIN_SAMPLES - 1)
        assert all(d["low_confidence"] for d in results.values())

    def test_not_flagged_at_or_above_floor(self):
        from src.eval.multi_agent_batch_eval import _annotate_low_confidence
        from src.eval.stats import MIN_SAMPLES

        results = {"helpfulness": {"score": 0.8}}
        _annotate_low_confidence(results, MIN_SAMPLES)
        assert results["helpfulness"]["low_confidence"] is False


class TestGetMetrics:
    def test_get_metrics_excludes_custom_policy_metric(self):
        # policy_compliance is NOT scored via client.evals (SDK-broken custom
        # pointwise metric — see src/eval/policy_judge.py); it must not appear in
        # the server-side rubric set for any agent, or every case errors.
        from src.eval.agent_eval_configs import get_metrics
        from src.eval.batch_eval import POLICY_COMPLIANCE_METRIC

        assert POLICY_COMPLIANCE_METRIC not in get_metrics("coordinator_agent")
        assert POLICY_COMPLIANCE_METRIC not in get_metrics("travel_agent")

    def test_get_metrics_has_six_rubrics(self):
        from src.eval.agent_eval_configs import get_metrics

        assert len(get_metrics("coordinator_agent")) == 6


class TestMultiTurnMetrics:
    """Multi-turn adaptive metrics live only on the client.evals surface and
    need multi-turn conversation data, so they attach to the simulated-eval
    path — not the single-turn 6-rubric batch."""

    def test_returns_three_multi_turn_metrics(self):
        from src.eval.agent_eval_configs import get_multi_turn_metrics

        assert len(get_multi_turn_metrics()) == 3

    def test_returns_the_expected_rubric_loaders(self):
        # RubricMetric.* returns a fresh loader instance per access with
        # identity-based equality, so assert on the stable loader .name.
        from src.eval.agent_eval_configs import get_multi_turn_metrics

        names = {m.name for m in get_multi_turn_metrics()}
        assert names == {
            "MULTI_TURN_TASK_SUCCESS",
            "MULTI_TURN_TOOL_USE_QUALITY",
            "MULTI_TURN_TRAJECTORY_QUALITY",
        }


class TestComplexityMetricDefined:
    def test_complexity_routing_metric_exists(self):
        from src.eval.complexity_metrics import COMPLEXITY_ROUTING_METRIC

        assert COMPLEXITY_ROUTING_METRIC is not None

    def test_check_complexity_routing_callable(self):

        from src.eval.complexity_metrics import check_complexity_routing

        assert callable(check_complexity_routing)


class TestEvalsetFilesValidJson:
    @pytest.mark.parametrize("filename", EVALSET_FILES)
    def test_evalset_parses(self, filename):
        path = EVALSETS_DIR / filename
        assert path.exists(), f"Evalset file not found: {path}"
        with open(path) as f:
            data = json.load(f)
        assert "eval_set_id" in data
        assert "eval_cases" in data
        assert len(data["eval_cases"]) > 0

    @pytest.mark.parametrize("filename", EVALSET_FILES)
    def test_evalset_cases_have_required_fields(self, filename):
        path = EVALSETS_DIR / filename
        with open(path) as f:
            data = json.load(f)
        for case in data["eval_cases"]:
            assert "eval_id" in case, f"Missing eval_id in {filename}"
            assert "conversation" in case, f"Missing conversation in {filename}"
            assert len(case["conversation"]) > 0
            turn = case["conversation"][0]
            assert "user_content" in turn
            assert "final_response" in turn
            assert "intermediate_data" in turn
            # intermediate_data is {} for no-tool-call cases (e.g. out-of-scope
            # replies the agent handles without any tool); when tool_uses is
            # present it must be a list of tool calls.
            assert isinstance(turn["intermediate_data"].get("tool_uses", []), list)

    @pytest.mark.parametrize("filename", EVALSET_FILES)
    def test_evalset_unique_eval_ids(self, filename):
        path = EVALSETS_DIR / filename
        with open(path) as f:
            data = json.load(f)
        ids = [c["eval_id"] for c in data["eval_cases"]]
        assert len(ids) == len(set(ids)), (
            f"Duplicate eval_ids in {filename}: {[x for x in ids if ids.count(x) > 1]}"
        )


class TestEvalConfigFiles:
    def test_static_eval_config(self):
        path = EVALSETS_DIR / "eval_config.json"
        assert path.exists()
        with open(path) as f:
            data = json.load(f)
        assert "criteria" in data
        assert "response_match_score" in data["criteria"]

    def test_dynamic_eval_config(self):
        path = SCENARIOS_DIR / "eval_config.json"
        assert path.exists()
        with open(path) as f:
            data = json.load(f)
        assert "criteria" in data
        assert "user_simulator_config" in data


class TestScenarioFiles:
    @pytest.mark.parametrize("filename", SCENARIO_FILES)
    def test_scenario_parses(self, filename):
        path = SCENARIOS_DIR / filename
        assert path.exists(), f"Scenario file not found: {path}"
        with open(path) as f:
            data = json.load(f)
        assert "scenarios" in data
        assert len(data["scenarios"]) > 0

    @pytest.mark.parametrize("filename", SCENARIO_FILES)
    def test_scenarios_have_required_fields(self, filename):
        path = SCENARIOS_DIR / filename
        with open(path) as f:
            data = json.load(f)
        for scenario in data["scenarios"]:
            assert "starting_prompt" in scenario, f"Missing starting_prompt in {filename}"
            assert "conversation_plan" in scenario, f"Missing conversation_plan in {filename}"
            assert "user_persona" in scenario, f"Missing user_persona in {filename}"

    def test_session_input_exists(self):
        path = SCENARIOS_DIR / "session_input.json"
        assert path.exists()
        with open(path) as f:
            data = json.load(f)
        assert "app_name" in data
        assert "user_id" in data

    def test_user_sim_config(self):
        path = SCENARIOS_DIR / "user_sim_config.json"
        assert path.exists()
        with open(path) as f:
            data = json.load(f)
        assert "count" in data
        assert "model_name" in data


class TestRouterEvalsetComplexityLevels:
    def test_has_all_complexity_levels(self):
        path = EVALSETS_DIR / "router_agent.evalset.json"
        with open(path) as f:
            data = json.load(f)
        levels = set()
        for case in data["eval_cases"]:
            complexity = case["conversation"][0]["intermediate_data"].get("expected_complexity")
            if complexity:
                levels.add(complexity)
        assert levels == {"low", "medium", "high"}, (
            f"Missing complexity levels: {{'low', 'medium', 'high'}} - {levels}"
        )

    def test_min_cases_per_level(self):
        path = EVALSETS_DIR / "router_agent.evalset.json"
        with open(path) as f:
            data = json.load(f)
        counts = {"low": 0, "medium": 0, "high": 0}
        for case in data["eval_cases"]:
            complexity = case["conversation"][0]["intermediate_data"].get("expected_complexity")
            if complexity in counts:
                counts[complexity] += 1
        for level, count in counts.items():
            assert count >= 2, f"Need at least 2 cases for {level}, got {count}"
