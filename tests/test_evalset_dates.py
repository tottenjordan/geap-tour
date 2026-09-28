"""Expected tool-argument dates in the saved evalsets follow the search inventory.

Since #174 the inventory and the agent both resolve "June 15" to its next
occurrence. The evalsets kept expecting 2026-06-15 after that date passed, naming
a date the agent would never search. These tests pin the rollover as of the day it
was made, and the check that warns when a saved date passes again.
"""

import inspect
import json
import os
from datetime import date
from pathlib import Path

import pytest

from src.eval import dataset_integrity, one_time_eval
from src.eval.dataset_integrity import past_tool_arg_dates, warn_on_past_tool_arg_dates
from src.mcp_servers.search.mock_db import next_occurrence

# The day the committed dates were rolled forward. Fixed so this suite does not go
# red on a calendar date; the runtime warning covers what happens after it.
ROLLED_ON = date(2026, 9, 28)

EVALSETS = sorted(str(p) for p in Path("src").rglob("*.evalset.json"))


def _dates(path):
    data = json.loads(Path(path).read_text())
    for case in data.get("eval_cases") or []:
        for turn in case.get("conversation") or []:
            for use in (turn.get("intermediate_data") or {}).get("tool_uses") or []:
                for value in (use.get("args") or {}).values():
                    if isinstance(value, str) and dataset_integrity._ISO_DATE.match(value):
                        yield date.fromisoformat(value)


def test_the_sweep_sees_the_evalsets():
    assert len(EVALSETS) >= 10


def test_no_committed_date_had_passed_when_rolled():
    assert past_tool_arg_dates(EVALSETS, ROLLED_ON) == []


def test_every_committed_date_is_the_inventorys_next_occurrence():
    """The dates the agent and the search server would produce on that day."""
    found = [(p, d) for p in EVALSETS for d in _dates(p)]
    assert found, "expected at least the June 15 cases"
    for path, d in found:
        assert d == next_occurrence(d.month, d.day, ROLLED_ON), (path, d)


def _evalset(tmp_path, args):
    path = tmp_path / "x.evalset.json"
    case = {
        "eval_id": "c1",
        "conversation": [{"intermediate_data": {"tool_uses": [{"name": "t", "args": args}]}}],
    }
    path.write_text(json.dumps({"eval_cases": [case]}))
    return path


class TestPastToolArgDates:
    def test_a_passed_date_is_named(self, tmp_path):
        path = _evalset(tmp_path, {"date": "2026-06-15"})
        assert past_tool_arg_dates([path], date(2026, 9, 28)) == [f"{path}: c1 t(date=2026-06-15)"]

    def test_today_and_later_are_not_stale(self, tmp_path):
        path = _evalset(tmp_path, {"date": "2026-06-15"})
        assert past_tool_arg_dates([path], date(2026, 6, 15)) == []

    @pytest.mark.parametrize("value", ["SFO", "2026-06", "June 15", 450])
    def test_non_dates_are_ignored(self, tmp_path, value):
        path = _evalset(tmp_path, {"x": value})
        assert past_tool_arg_dates([path], date(2030, 1, 1)) == []

    def test_warning_is_printed_not_raised(self, tmp_path, capsys):
        path = _evalset(tmp_path, {"date": "2026-06-15"})
        warn_on_past_tool_arg_dates([path], date(2026, 9, 28))
        assert "date has passed" in capsys.readouterr().out


class TestTheCheckRunsWhereTheEvalsetsAreUsed:
    def test_one_time_eval_checks_its_evalset(self):
        src = inspect.getsource(one_time_eval.run_one_time_eval)
        assert "warn_on_past_tool_arg_dates([evalset_file])" in src

    @pytest.mark.parametrize(
        ("module_path", "sampler"),
        [
            ("src/agents/coordinator", "src/optimize/sampler_config.json"),
            ("src/router/flash_agent_opt", "src/optimize/flash_sampler_config.json"),
        ],
    )
    def test_run_optimize_path_points_at_a_real_train_set(self, module_path, sampler):
        """run_optimize rebuilds the path LocalEvalSetsManager reads; it must exist,
        or the warning would silently check nothing."""
        train = json.loads(Path(sampler).read_text())["train_eval_set"]
        assert os.path.exists(os.path.join(module_path, f"{train}.evalset.json"))
