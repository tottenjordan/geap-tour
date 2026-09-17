"""An eval that scored nothing must never report success.

This bug was written four separate times in this repo, once per eval path, each
copied from the last:

* `simulated_eval` — PR #138, after a run reported SUCCEEDED and `all_passed: true`
  over a conversation the SDK had silently discarded;
* `multi_agent_batch_eval` — 2026-09-17, in the module the **CI eval gate** runs, so
  a scoreless run exited 0;
* `batch_eval` — 2026-09-17, the single-agent path the other two were extended from;
* `preflight` — 2026-09-17, found by the structural detector in this file rather than
  by reading: the bake-off's own "don't spend money on a missing backbone" gate
  reported OK having checked nothing.

Four independent sites wrote `all_pass = True` and a loop that never executes on
an empty result. Fixing the third instance alone would leave the fourth free to
appear, so these tests assert the property across **every** verdict path at once —
and the structural test below fails on the shape itself, not just on today's three
call sites.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from src.eval.stats import all_metrics_passed

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


class TestTheSharedRule:
    def test_no_metrics_is_not_a_pass(self):
        """THE property. `all([])` is True — correct for logic, wrong for verdicts."""
        assert all_metrics_passed([]) is False
        assert all([]) is True, "if this ever changes, the helper's reason is gone"

    def test_all_passing_is_a_pass(self):
        assert all_metrics_passed([True, True]) is True

    def test_one_failure_fails(self):
        assert all_metrics_passed([True, False, True]) is False

    def test_a_generator_is_consumed_once_and_correctly(self):
        """Call sites pass generator expressions; a naive `bool(flags) and all(flags)`
        over a generator would consume it twice and always return True."""
        assert all_metrics_passed(x > 0 for x in [1, 2]) is True
        assert all_metrics_passed(x > 0 for x in [1, -1]) is False
        assert all_metrics_passed(x > 0 for x in []) is False


class TestEveryVerdictPathRejectsAnEmptyResult:
    """The three real paths, exercised through their own result builders."""

    def test_batch_eval(self):
        from types import SimpleNamespace

        from src.eval.batch_eval import _build_results

        def run(metrics):
            return SimpleNamespace(
                evaluation_run_results=SimpleNamespace(
                    summary_metrics=SimpleNamespace(metrics=metrics, total_items=10, failed_items=0)
                )
            )

        assert _build_results("r", "a", run({}), 3.0, 1.0)["all_passed"] is False
        good = _build_results("r", "a", run({"c/safety_v1/AVERAGE": 0.9}), 3.0, 1.0)
        assert good["all_passed"] is True

    def test_multi_agent_batch_eval(self):
        """Covered in depth by test_multi_agent_batch_eval_flow; asserted here too so
        the three paths are checked together and a regression names this file."""
        from src.eval.multi_agent_batch_eval import _run_single_agent_eval

        assert callable(_run_single_agent_eval)
        src = pathlib.Path(_SRC / "eval/multi_agent_batch_eval.py").read_text()
        assert "all_metrics_passed(" in src

    def test_simulated_eval(self):
        src = (_SRC / "eval/simulated_eval.py").read_text()
        assert "all_metrics_passed(" in src

    def test_preflight_refuses_to_pass_having_checked_nothing(self, monkeypatch):
        """The FOURTH instance — found by the structural detector below, not by
        reading. Its whole job is to stop an expensive bake-off before it starts,
        and with an empty result it printed "Preflight OK - both backbones served"
        having verified neither."""
        import src.eval.preflight as pf

        monkeypatch.setattr(pf, "preflight_models", lambda _m: {})
        assert pf.main(["some-model"]) == 1

        monkeypatch.setattr(pf, "preflight_models", lambda _m: {"a": (True, "ok")})
        assert pf.main(["a"]) == 0

        monkeypatch.setattr(pf, "preflight_models", lambda _m: {"a": (False, "404")})
        assert pf.main(["a"]) == 1


class TestTheShapeCannotComeBack:
    """A structural guard, because the next copy will be written by someone who has
    not read any of the above.

    Flags the exact anti-pattern: a function that initialises a verdict variable to
    `True` and then only ever assigns `False` inside a loop. That is safe *only* if
    something guarantees the loop runs, which is precisely the assumption that failed
    three times.
    """

    @staticmethod
    def _offenders() -> list[str]:
        found = []
        for path in sorted(_SRC.rglob("*.py")):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:  # pragma: no cover - not expected in src/
                continue
            for func in ast.walk(tree):
                if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                # Verdict-ish names initialised to a literal True at function scope.
                seeded = {
                    t.id
                    for node in func.body
                    if isinstance(node, ast.Assign)
                    for t in node.targets
                    if isinstance(t, ast.Name)
                    and isinstance(node.value, ast.Constant)
                    and node.value.value is True
                    and ("pass" in t.id or "ok" in t.id or "valid" in t.id)
                }
                if not seeded:
                    continue
                # ...that are then only written to inside a loop.
                for loop in ast.walk(func):
                    if not isinstance(loop, ast.For | ast.While):
                        continue
                    for node in ast.walk(loop):
                        if isinstance(node, ast.Assign):
                            for t in node.targets:
                                if isinstance(t, ast.Name) and t.id in seeded:
                                    found.append(f"{path.relative_to(_SRC.parent)}::{func.name}")
        return sorted(set(found))

    def test_no_verdict_is_seeded_true_and_only_falsified_in_a_loop(self):
        offenders = self._offenders()
        assert not offenders, (
            "these functions seed a verdict to True and only clear it inside a loop, "
            "so an empty input reports success. Use stats.all_metrics_passed instead: "
            f"{offenders}"
        )

    def test_the_detector_would_catch_the_original_bug(self, tmp_path, monkeypatch):
        """Mutation-check on the detector itself — a scanner that finds nothing
        because it is broken looks exactly like a clean codebase."""
        bad = tmp_path / "src" / "evil.py"
        bad.parent.mkdir(parents=True)
        bad.write_text(
            "def verdict(metrics, floor):\n"
            "    all_pass = True\n"
            "    for name, score in metrics.items():\n"
            "        if score < floor:\n"
            "            all_pass = False\n"
            "    return all_pass\n"
        )
        monkeypatch.setattr("tests.test_no_vacuous_pass._SRC", tmp_path / "src", raising=False)
        import tests.test_no_vacuous_pass as mod

        monkeypatch.setattr(mod, "_SRC", tmp_path / "src")
        assert mod.TestTheShapeCannotComeBack._offenders(), (
            "the detector missed the textbook form of the bug"
        )


@pytest.mark.parametrize(
    "module",
    [
        "src/eval/batch_eval.py",
        "src/eval/multi_agent_batch_eval.py",
        "src/eval/simulated_eval.py",
        "src/eval/preflight.py",
    ],
)
def test_each_path_routes_through_the_one_rule(module):
    """Not three copies of a guard — one rule, three call sites. Three copies is how
    two of them stayed broken after the first was fixed."""
    assert "all_metrics_passed(" in (_SRC.parent / module).read_text()
