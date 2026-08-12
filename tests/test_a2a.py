"""Offline tests for the A2A integration (agent card, remote client, registry).

No credentials, live GCP, or network required — the A2A/registry preview
surface is stubbed/monkeypatched throughout, and the important cases assert
graceful degradation when that surface is unavailable.
"""

import logging
from unittest.mock import MagicMock

import pytest

from src.a2a.agent_card import (
    SKILL_IDS,
    agent_card_dict,
    build_agent_card,
    serialize_agent_card,
)
from src.a2a.remote_agent import (
    A2AUnavailable,
    build_remote_coordinator,
    try_build_remote_coordinator,
)


class TestAgentCard:
    def test_name_is_coordinator_agent(self):
        card = build_agent_card()
        assert card.name == "coordinator_agent"

    def test_expected_skills_present(self):
        card = build_agent_card()
        ids = {s.id for s in card.skills}
        assert ids == {
            "flight_search",
            "hotel_search",
            "booking",
            "expense_policy_check",
            "expense_submission",
        }
        assert set(SKILL_IDS) == ids

    def test_skill_names_are_human_readable(self):
        card = build_agent_card()
        by_id = {s.id: s.name for s in card.skills}
        assert by_id["flight_search"] == "Flight Search"
        assert by_id["expense_submission"] == "Expense Submission"

    def test_url_override_flows_into_interface(self):
        card = build_agent_card(url="https://custom.example.com/a2a")
        urls = [i.url for i in card.supported_interfaces]
        assert "https://custom.example.com/a2a" in urls

    def test_default_url_derives_from_config(self):
        card = build_agent_card()
        urls = [i.url for i in card.supported_interfaces]
        # Derived from GCP project/region/engine id in config.
        assert any("reasoningEngines" in u for u in urls)

    def test_serializes_to_dict(self):
        d = agent_card_dict()
        assert d["name"] == "coordinator_agent"
        assert isinstance(d["skills"], list)
        skill_ids = {s["id"] for s in d["skills"]}
        assert "flight_search" in skill_ids

    def test_serialize_agent_card_pydantic_fallback(self):
        # Older a2a-sdk exposes model_dump; make sure that path is honored.
        fake = MagicMock()
        fake.model_dump.return_value = {"name": "x"}
        assert serialize_agent_card(fake) == {"name": "x"}
        fake.model_dump.assert_called_once()


class _StubRemoteAgent:
    """Records the constructor args so tests can assert the target URL."""

    def __init__(self, *, name, agent_card, description):
        self.name = name
        self.agent_card = agent_card
        self.description = description


class TestRemoteAgent:
    def test_builds_against_stubbed_url(self, monkeypatch):
        monkeypatch.setattr(
            "src.a2a.remote_agent._load_remote_agent_class",
            lambda: _StubRemoteAgent,
        )
        agent = build_remote_coordinator(url="https://stub.example.com")
        assert isinstance(agent, _StubRemoteAgent)
        assert agent.name == "coordinator_agent"
        # The well-known agent-card path is appended to the base URL.
        assert agent.agent_card.startswith("https://stub.example.com")
        assert "agent-card.json" in agent.agent_card

    def test_real_class_constructs_offline(self):
        # The genuine ADK RemoteA2aAgent resolves its card lazily, so building
        # against a URL is offline-safe (no network at construction).
        agent = build_remote_coordinator(url="https://real.example.com")
        assert agent is not None
        assert "real.example.com" in getattr(agent, "_agent_card_source", "")

    def test_import_failure_raises_a2a_unavailable(self, monkeypatch):
        def _boom():
            raise ImportError("no preview here")

        monkeypatch.setattr(
            "src.a2a.remote_agent._load_remote_agent_class", _boom
        )
        with pytest.raises(A2AUnavailable):
            build_remote_coordinator(url="https://x.example.com")

    def test_try_build_returns_none_and_logs_skip(self, monkeypatch, caplog):
        def _boom():
            raise ImportError("no preview here")

        monkeypatch.setattr(
            "src.a2a.remote_agent._load_remote_agent_class", _boom
        )
        with caplog.at_level(logging.WARNING):
            result = try_build_remote_coordinator(url="https://x.example.com")
        assert result is None
        assert "A2A preview not enabled — skipping" in caplog.text

    def test_construction_error_degrades(self, monkeypatch):
        def _bad_cls(**kwargs):
            raise ValueError("incompatible signature")

        monkeypatch.setattr(
            "src.a2a.remote_agent._load_remote_agent_class", lambda: _bad_cls
        )
        assert try_build_remote_coordinator(url="https://x.example.com") is None


class TestRegistry:
    def test_register_a2a_agent_calls_client(self, monkeypatch):
        import src.registry as registry

        fake_registry = MagicMock()
        fake_registry._make_request.return_value = {"name": "projects/p/.../agents/a"}
        monkeypatch.setattr(registry, "get_registry", lambda: fake_registry)

        card = build_agent_card()
        result = registry.register_a2a_agent(card)

        assert result == {"name": "projects/p/.../agents/a"}
        fake_registry._make_request.assert_called_once()
        args, kwargs = fake_registry._make_request.call_args
        # Path targets the agents collection with the derived agent id.
        assert args[0].startswith("agents?agentId=coordinator-agent")
        assert kwargs["method"] == "POST"
        body = kwargs["json_data"]
        assert body["displayName"] == "coordinator_agent"
        assert body["card"]["type"] == "A2A_AGENT_CARD"
        assert body["card"]["content"]["name"] == "coordinator_agent"

    def test_register_accepts_dict_card(self, monkeypatch):
        import src.registry as registry

        fake_registry = MagicMock()
        fake_registry._make_request.return_value = {"name": "ok"}
        monkeypatch.setattr(registry, "get_registry", lambda: fake_registry)

        result = registry.register_a2a_agent(
            {"name": "coordinator_agent", "description": "d"}
        )
        assert result == {"name": "ok"}

    def test_get_a2a_agents_returns_list(self, monkeypatch):
        import src.registry as registry

        fake_registry = MagicMock()
        fake_registry.list_agents.return_value = {"agents": [{"name": "a"}, {"name": "b"}]}
        monkeypatch.setattr(registry, "get_registry", lambda: fake_registry)

        agents = registry.get_a2a_agents()
        assert agents == [{"name": "a"}, {"name": "b"}]

    def test_register_degrades_on_error(self, monkeypatch, caplog):
        import src.registry as registry

        def _boom():
            raise RuntimeError("preview surface 404")

        monkeypatch.setattr(registry, "get_registry", _boom)
        with caplog.at_level(logging.INFO):
            result = registry.register_a2a_agent(build_agent_card())
        assert result is None
        assert "A2A preview not enabled — skipping" in caplog.text

    def test_discover_degrades_on_error(self, monkeypatch):
        import src.registry as registry

        def _boom():
            raise RuntimeError("preview surface 404")

        monkeypatch.setattr(registry, "get_registry", _boom)
        assert registry.get_a2a_agents() == []


class TestRegisterCli:
    def test_register_cli_degrades_and_exits_zero(self, monkeypatch, caplog):
        import src.deploy.register_a2a as cli

        # Simulate the registry helper finding no preview surface.
        monkeypatch.setattr(cli, "register_a2a_agent", lambda card: None)
        with caplog.at_level(logging.INFO):
            rc = cli.main([])
        assert rc == 0
        assert "A2A preview not enabled — skipping" in caplog.text

    def test_register_cli_does_not_raise_on_helper_exception(self, monkeypatch):
        import src.deploy.register_a2a as cli

        def _boom(card):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(cli, "register_a2a_agent", _boom)
        # Must not propagate — the CLI's outer guard catches it and exits 0.
        assert cli.main([]) == 0

    def test_discover_cli_lists_agents(self, monkeypatch, capsys):
        import src.deploy.register_a2a as cli

        monkeypatch.setattr(
            cli, "get_a2a_agents", lambda: [{"name": "n1", "displayName": "Coord"}]
        )
        rc = cli.main(["--discover"])
        assert rc == 0
        assert "n1" in capsys.readouterr().out

    def test_discover_cli_degrades_when_empty(self, monkeypatch, caplog):
        import src.deploy.register_a2a as cli

        monkeypatch.setattr(cli, "get_a2a_agents", list)
        with caplog.at_level(logging.INFO):
            rc = cli.main(["--discover"])
        assert rc == 0
        assert "A2A preview not enabled — skipping" in caplog.text
