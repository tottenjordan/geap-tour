"""PROMPT_VARIANT selects the GEPA vs baseline instruction for the sub-agents.

travel_agent/expense_agent bind INSTRUCTION from PROMPT_VARIANT at import time,
so switching variants requires reloading src.config + the agent module. A fixture
reloads with clean env on teardown to avoid cross-module pollution.
"""

import importlib

import pytest

import src.config
import src.agents.travel_agent as travel_mod
import src.agents.expense_agent as expense_mod


def _reload():
    importlib.reload(src.config)
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
