"""PROMPT_VARIANT selects the GEPA vs baseline instruction for the sub-agents.

travel_agent/expense_agent bind INSTRUCTION from PROMPT_VARIANT at import time,
so switching variants requires reloading src.config + the agent module. A fixture
reloads with clean env on teardown to avoid cross-module pollution.

src.registry must be reloaded between src.config and the agent modules: it binds
MCP_SERVER_URLS from src.config at import time, and the agent modules resolve
tools through registry's URL fallback. Without a registry reload the fallback map
is keyed by the *collection-time* (pre-conftest) server names, so a credential-
less environment (CI) misses the fallback and re-raises. See the URL fallback in
src/registry.py:get_mcp_tools.
"""

import importlib

import pytest

import src.agents.expense_agent as expense_mod
import src.agents.travel_agent as travel_mod
import src.config
import src.registry


def _reload():
    importlib.reload(src.config)
    importlib.reload(src.registry)
    return importlib.reload(travel_mod), importlib.reload(expense_mod)


@pytest.fixture
def reloaded_agents(monkeypatch):
    """Yield a reload helper; restore clean defaults on teardown."""
    yield _reload
    monkeypatch.delenv("PROMPT_VARIANT", raising=False)
    _reload()


def test_default_is_gepa(reloaded_agents, monkeypatch):
    monkeypatch.delenv("PROMPT_VARIANT", raising=False)
    travel, expense = reloaded_agents()

    assert travel.INSTRUCTION == travel.INSTRUCTION_GEPA
    assert expense.INSTRUCTION == expense.INSTRUCTION_GEPA


def test_baseline_selected(reloaded_agents, monkeypatch):
    monkeypatch.setenv("PROMPT_VARIANT", "baseline")
    travel, expense = reloaded_agents()

    assert travel.INSTRUCTION == travel.INSTRUCTION_BASELINE
    assert expense.INSTRUCTION == expense.INSTRUCTION_BASELINE
    # The two variants must actually differ so the experiment measures something.
    assert travel.INSTRUCTION_BASELINE != travel.INSTRUCTION_GEPA
    assert expense.INSTRUCTION_BASELINE != expense.INSTRUCTION_GEPA


def test_gepa_selected(reloaded_agents, monkeypatch):
    monkeypatch.setenv("PROMPT_VARIANT", "gepa")
    travel, expense = reloaded_agents()

    assert travel.INSTRUCTION == travel.INSTRUCTION_GEPA
    assert expense.INSTRUCTION == expense.INSTRUCTION_GEPA


def test_unknown_variant_falls_back_to_gepa(reloaded_agents, monkeypatch):
    monkeypatch.setenv("PROMPT_VARIANT", "nonsense")
    travel, expense = reloaded_agents()

    assert travel.INSTRUCTION == travel.INSTRUCTION_GEPA
    assert expense.INSTRUCTION == expense.INSTRUCTION_GEPA
