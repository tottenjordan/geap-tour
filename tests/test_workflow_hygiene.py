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
import re

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


def _triggers(path: pathlib.Path) -> dict:
    """The `on:` block — under the key `True`, not `"on"`.

    YAML 1.1 parses a bare `on` as the boolean true, so `workflow["on"]` raises
    KeyError on every GitHub workflow ever written. Solved once here rather than
    rediscovered per test (test_monitoring_publish.py carries its own copy).
    """
    data = _load(path)
    return data.get(True) or data.get("on") or {}


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


class TestTheRouterQualityWorkflow:
    """agent_router_quality/* shipped with four alert policies and no writer.

    `src/eval/baseline.py:MIN_BASELINE` is 5, so a series stuck at n=1 has policies
    watching something that can never move — the same state `agent_eval/tool_faithfulness`
    sat in for days (#84), and then its online twin. The four parametrized guards above
    cover this file automatically (they glob the directory); these are the properties
    specific to what it publishes.
    """

    PATH = pathlib.Path(__file__).resolve().parents[1] / ".github/workflows/router_quality.yaml"

    def test_it_exists(self):
        assert self.PATH.is_file(), "agent_router_quality/* has alerts but no scheduled writer"

    def test_it_runs_daily_not_hourly(self):
        """Hourly is the wrong cadence, not merely an expensive one: this makes real
        engine inference and judge calls, unlike the classifier-only efficiency
        publisher that shares the hourly workflow."""
        cron = _triggers(self.PATH)["schedule"][0]["cron"]
        minute, hour = cron.split()[0], cron.split()[1]
        assert hour != "*", f"{cron!r} runs hourly — this publisher costs engine calls"
        assert minute not in ("0", "30"), f"{cron!r} sits in GitHub's most contended slot"

    def test_the_sample_clears_the_low_confidence_floor(self):
        """`stats.MIN_SAMPLES` is 8. A smaller run publishes a point the harness itself
        flags `low_confidence`, which is a poor thing to build a baseline out of."""
        from src.eval.stats import MIN_SAMPLES

        run = "\n".join(s.get("run", "") for s in _load(self.PATH)["jobs"]["publish"]["steps"])
        match = re.search(r"--limit (\d+)", run)
        assert match, "the publish step must bound its sample with --limit"
        assert int(match.group(1)) >= MIN_SAMPLES

    def test_it_targets_the_router_engine(self):
        """Publishing the COORDINATOR's scores into agent_router_quality/* would be
        silent and completely wrong — and the repo has already shipped an engine-id
        mixup once (the 2026-08-21 AGENT_ENGINE_ID drift)."""
        text = self.PATH.read_text()
        assert "ROUTER_ENGINE_ID" in text
        assert "vars.AGENT_ENGINE_ID" not in text

    def test_the_publish_step_is_not_advisory(self):
        """The CLI already exits 1 when nothing was published; `continue-on-error`
        would throw that away and a permanently broken daily publish would read green —
        the precise failure this workflow exists to end."""
        steps = _load(self.PATH)["jobs"]["publish"]["steps"]
        publish = [s for s in steps if "publish_router_quality" in s.get("run", "")]
        assert publish, "no publish step found"
        assert all(not s.get("continue-on-error") for s in publish)

    def test_the_engine_is_verified_before_it_is_scored(self):
        """A second copy of an engine id drifts (AGENT_ENGINE_ID did). Checking the
        role first turns a wrong id into a loud failure instead of another engine's
        scores landing in this series."""
        steps = _load(self.PATH)["jobs"]["publish"]["steps"]
        runs = [s.get("run", "") for s in steps]
        verify = next(i for i, r in enumerate(runs) if "verify_engine_config" in r)
        publish = next(i for i, r in enumerate(runs) if "publish_router_quality" in r)
        assert verify < publish
        assert "--role router" in runs[verify]
