"""Properties every CI workflow must hold, checked on the committed YAML.

Workflows are the least-tested code in most repos: nothing type-checks them, and a
mistake shows up as a job that quietly costs money, blocks a runner, or never runs.
This repo already learned that twice — `eval_gate` sat `skipped` for 15 invocations
because it waited on a label nobody applied, and `tool_faithfulness` published
nothing for days without an error.

These are cheap structural assertions, sized from real run data rather than taste:

    workflow                 median   max
    tests.yaml                  84s    108s
    eval_gate.yaml             363s    363s
    monitoring_publish.yaml   1068s   1338s
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

_WORKFLOWS = sorted(
    (pathlib.Path(__file__).resolve().parents[1] / ".github/workflows").glob("*.yaml")
)
_IDS = [p.name for p in _WORKFLOWS]


def _load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


def _jobs(path: pathlib.Path):
    return _load(path).get("jobs", {}).items()


@pytest.mark.parametrize("path", _WORKFLOWS, ids=_IDS)
class TestEveryWorkflow:
    def test_every_job_has_a_timeout(self, path):
        """GitHub's default is 360 MINUTES. A hung job holds a runner for six hours
        and tells nobody — and three of these workflows make live network calls
        whose individual requests are not bounded by anything."""
        missing = [n for n, j in _jobs(path) if j.get("timeout-minutes") is None]
        assert not missing, f"{path.name}: jobs without timeout-minutes: {missing}"

    def test_the_timeout_is_not_a_rubber_stamp(self, path):
        """A 360-minute 'timeout' is the default wearing a disguise."""
        for name, job in _jobs(path):
            assert job["timeout-minutes"] < 120, (
                f"{path.name}:{name} sets {job['timeout-minutes']}min — longer than "
                "any observed run by an order of magnitude, so it protects nothing"
            )

    def test_every_job_declares_permissions(self, path):
        """Least privilege. Without an explicit block the job inherits the repo
        default, which can be write-all — on a workflow that only reads code."""
        missing = [n for n, j in _jobs(path) if not j.get("permissions")]
        assert not missing, f"{path.name}: jobs without explicit permissions: {missing}"

    def test_concurrency_is_declared(self, path):
        """Without it, superseded runs finish anyway: three pushes to a PR run three
        full suites, and two dispatches submit two billable pipelines."""
        assert _load(path).get("concurrency"), f"{path.name} has no concurrency group"


class TestTheTestWorkflowSpecifically:
    PATH = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/tests.yaml"

    def _steps(self):
        return _load(self.PATH)["jobs"]["tests"]["steps"]

    def _run_text(self):
        return "\n".join(s.get("run", "") for s in self._steps())

    def test_it_syncs_the_optional_groups(self):
        """tests/conftest.py drops 39 tests when pyDOE3/kfp are absent. Since
        2026-09-17 that is a hard error in CI rather than a smaller green run — but
        catching it here means the PR fails on the workflow edit, not on the next
        scheduled run."""
        text = self._run_text()
        assert "--group doe" in text and "--group pipelines" in text

    def test_parallel_runs_pin_distribution_to_whole_files(self):
        """`-n` without `--dist loadfile` splits a module across workers, and ~10
        modules here reload src.config / src.registry and mutate module-level state.
        The failure would be an intermittent, unreproducible red."""
        text = self._run_text()
        if "-n " in text or "-n=" in text:
            assert "--dist loadfile" in text, (
                "parallel test execution must keep each module on one worker"
            )

    def test_the_run_step_does_not_re_resolve_the_environment(self):
        """A bare `uv run` re-resolves and can install a different set than the sync
        step just verified — which is exactly how the 39-test collection drop
        happened locally."""
        run_steps = [s.get("run", "") for s in self._steps() if "pytest" in s.get("run", "")]
        assert run_steps, "no pytest step found"
        for step in run_steps:
            assert "--no-sync" in step, f"pytest step re-resolves the env: {step!r}"

    def test_the_serial_choice_carries_its_measurement(self):
        """`-n auto` is the obvious optimization and it is WRONG here — measured 69s
        serial vs 74s on 4 workers, because each worker re-pays the ADK import and
        there is nothing to amortize in a 69s run.

        A bare `run: pytest` looks like nobody thought about it, so the next person
        adds `-n auto`, sees it pass, and ships a slowdown. The numbers live beside
        the command; this asserts they stay there.
        """
        text = self.PATH.read_text()
        assert "SERIAL ON PURPOSE" in text
        assert "69s" in text and "74s" in text, (
            "the measurement justifying serial execution must stay next to the command"
        )

    def test_main_pushes_are_not_cancelled(self):
        """Cancelling a main-branch run because another merge landed leaves a commit
        with no green record. Only PR runs should supersede each other."""
        cancel = str(_load(self.PATH)["concurrency"]["cancel-in-progress"])
        assert "pull_request" in cancel, (
            "cancel-in-progress must be conditional on the event, not unconditional"
        )
