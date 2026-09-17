"""A suite that silently shrinks must not look like a suite that passed.

`tests/conftest.py` uses `collect_ignore` to drop the DOE/pipeline test modules when
their optional dependency groups are absent. That is the right call — without it a
bare local run dies with "Interrupted: N errors during collection" instead of running
the other 1920 tests. But `collect_ignore` makes modules *vanish*: no skip marker, no
skip count, no warning. The run just reports a smaller number.

Measured exposure: 1959 collected with `--all-groups`, 1920 without. **39 tests**
disappear with nothing in the output saying so — the same number CLAUDE.md warns about
in prose, which is the tell that someone already lost time to it.

This is the house failure mode in a new costume: a green signal computed over a
silently reduced population (cf. `agent_router/*` alerting on a series with no writer,
`engine_baseline` reporting a plugin ACTIVE because a flag was set). So the ignore is
now announced in the report header, and in CI it is a hard error.
"""

from __future__ import annotations

import sys

import pytest

# pytest imports the conftest under a rootdir-relative name ("tests.conftest" here,
# bare "conftest" on older layouts). Resolve it rather than guessing, so this file
# does not break on a pytest/layout change in a way that looks like a real failure.
conftest = next(
    mod
    for name, mod in sys.modules.items()
    if name.rsplit(".", 1)[-1] == "conftest" and hasattr(mod, "_OPTIONAL_DEP_MODULES")
)


class TestTheIgnoredModulesAreAnnounced:
    def test_header_is_silent_when_nothing_was_dropped(self, monkeypatch):
        """No warning on a complete run — a banner that always prints gets ignored."""
        monkeypatch.setattr(conftest, "_IGNORED_MODULES", [])
        assert conftest.pytest_report_header(config=None) is None

    def test_header_names_the_modules_and_the_missing_groups(self, monkeypatch):
        monkeypatch.setattr(conftest, "_IGNORED_MODULES", ["test_doe_run.py", "test_pipeline.py"])
        header = conftest.pytest_report_header(config=None)
        text = "\n".join(header)

        assert "NOT COLLECTED" in text
        assert "test_doe_run.py" in text and "test_pipeline.py" in text
        # The distinction that matters: ignored != skipped.
        assert "not skipped" in text
        assert "uv sync --all-groups" in text, "the header must say how to fix it"

    def test_the_header_names_the_dependency_not_just_the_module(self, monkeypatch):
        """`test_pipeline.py was ignored` is not actionable; `kfp is missing` is."""
        monkeypatch.setattr(conftest, "_IGNORED_MODULES", ["test_pipeline.py"])
        assert "kfp" in "\n".join(conftest.pytest_report_header(config=None))


class TestCIRefusesAPartialSuite:
    def test_ci_errors_when_modules_were_dropped(self, monkeypatch):
        """THE property. A partial run in CI is not a smaller pass, it is no result.

        Without this, deleting one `--group` from tests.yaml drops 39 tests and the
        check still goes green.
        """
        monkeypatch.setattr(conftest, "_IGNORED_MODULES", ["test_doe_run.py"])
        monkeypatch.setenv("CI", "true")
        monkeypatch.delenv("ALLOW_PARTIAL_TEST_RUN", raising=False)

        with pytest.raises(pytest.UsageError) as exc:
            conftest.pytest_sessionstart(session=None)
        assert "PARTIAL suite" in str(exc.value)
        assert "pyDOE3" in str(exc.value), "the error must name the missing group"

    def test_ci_is_quiet_when_the_suite_is_complete(self, monkeypatch):
        monkeypatch.setattr(conftest, "_IGNORED_MODULES", [])
        monkeypatch.setenv("CI", "true")
        assert conftest.pytest_sessionstart(session=None) is None

    def test_local_runs_are_not_blocked(self, monkeypatch):
        """A dev without the optional groups must still be able to run the suite;
        they get the header instead. Turning this into a hard local failure would
        just teach people to pass the opt-out permanently."""
        monkeypatch.setattr(conftest, "_IGNORED_MODULES", ["test_doe_run.py"])
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("ALLOW_PARTIAL_TEST_RUN", raising=False)
        assert conftest.pytest_sessionstart(session=None) is None

    def test_the_opt_out_works(self, monkeypatch):
        monkeypatch.setattr(conftest, "_IGNORED_MODULES", ["test_doe_run.py"])
        monkeypatch.setenv("CI", "true")
        monkeypatch.setenv("ALLOW_PARTIAL_TEST_RUN", "1")
        assert conftest.pytest_sessionstart(session=None) is None


class TestTheGateIsWiredToRealModules:
    def test_every_gated_module_exists(self):
        """A renamed test file would silently stop being gated — and then a bare run
        aborts on a collection error again, which is what the gate exists to avoid."""
        import pathlib

        tests_dir = pathlib.Path(__file__).parent
        missing = [m for m in conftest._OPTIONAL_DEP_MODULES if not (tests_dir / m).is_file()]
        assert not missing, f"conftest gates modules that no longer exist: {missing}"

    def test_this_run_collected_everything(self):
        """Belt and braces: whatever environment this is, say so out loud.

        Deliberately not asserting a hard-coded total — that would need editing on
        every new test. It asserts the *population*, which is the thing that was
        silently variable.
        """
        assert conftest._IGNORED_MODULES == [], (
            "this run is missing test modules: "
            f"{conftest._IGNORED_MODULES} (install: {conftest._missing_deps()})"
        )
