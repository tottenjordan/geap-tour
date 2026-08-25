"""The scheduled monitoring workflow's load-bearing details.

Workflows are the least-tested code in most repos and the easiest place for a
silent regression: nothing type-checks them, and a mistake only shows up as
metrics that quietly stop arriving — which is exactly what happened to
tool_faithfulness, whose series went empty for days without a single error.

These are cheap structural assertions on the committed YAML, not a CI simulation.
"""

from pathlib import Path

import pytest

# Imported directly, NOT via importorskip: PyYAML is present in every dependency
# group this repo syncs (transitively, 6.0.3), and a silently-skipped guard is
# worse than a missing one — it reads as "passing" in the CI summary.
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = _REPO_ROOT / ".github/workflows/monitoring_publish.yaml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def steps(workflow):
    return workflow["jobs"]["publish"]["steps"]


def _load_steps():
    """Steps list, for tests that don't take the module-scoped fixture."""
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["publish"]["steps"]


def _step(steps, needle):
    return next(s for s in steps if needle in (s.get("name") or ""))


class TestFaithfulnessIsActuallyPublished:
    """The metric must reach Cloud Monitoring, or its alert can never fire.

    History worth keeping: the bridge was run with --no-faithfulness to avoid
    overwriting a "deliberate RED demo point", and the series went EMPTY as a
    result — the hallucinated-action detector, the most safety-relevant metric
    here, had no data for days. The RED is produced on demand seconds before a
    demo and was never a persisted artifact, so nothing was being protected.
    """

    def test_faithfulness_has_its_own_publishing_step(self):
        assert _step(_load_steps(), "Publish tool-call faithfulness")

    def test_that_step_actually_publishes(self):
        run = _step(_load_steps(), "Publish tool-call faithfulness")["run"]
        assert "tool_faithfulness" in run
        assert "--publish" in run
        assert "--dry-run" not in run, "a dry run would leave the series empty"

    def test_the_run_is_bounded(self):
        """It is the priciest judge — one trajectory capture plus one judge call
        per case. Unbounded hourly would be the reason someone disables it again."""
        run = _step(_load_steps(), "Publish tool-call faithfulness")["run"]
        assert "--limit" in run

    def test_the_bridge_still_skips_it_to_avoid_double_publishing(self):
        """Two publishers on one series would double-write every hour. The bridge
        keeps --no-faithfulness, but now for THIS reason, not to protect a demo."""
        run = _step(_load_steps(), "Publish offline quality")["run"]
        assert "--no-faithfulness" in run

    def test_the_separation_is_explained_in_the_file(self):
        """A future editor deleting either half should hit the reason first."""
        text = WORKFLOW.read_text()
        assert "--no-faithfulness" in text
        assert "never fire" in text


class TestScheduling:
    def test_runs_on_a_schedule_and_on_demand(self, workflow):
        triggers = workflow.get(True) or workflow.get("on")
        assert "schedule" in triggers
        assert "workflow_dispatch" in triggers, "needs a manual trigger for backfills"

    def test_the_cron_avoids_the_top_of_the_hour(self, workflow):
        """GitHub delays scheduled runs under load and :00 is the worst slot."""
        triggers = workflow.get(True) or workflow.get("on")
        minute = triggers["schedule"][0]["cron"].split()[0]
        assert minute not in ("0", "30"), f"cron minute {minute} is a contended slot"

    def test_runs_do_not_overlap(self, workflow):
        """A run takes ~5 min; an overlapping one would double-publish the series."""
        assert workflow["concurrency"]["group"]
        assert workflow["concurrency"]["cancel-in-progress"] is False


class TestFailureBehaviour:
    def test_publish_steps_do_not_abort_each_other(self, steps):
        """One dead surface must not hide the other's result in the summary."""
        for name in ("Publish offline quality", "Publish online quality"):
            assert _step(steps, name)["continue-on-error"] is True

    def test_the_job_still_goes_red_when_everything_failed(self, steps):
        """continue-on-error everywhere would make a permanently-broken publish
        look green forever in the Actions list — the exact failure mode this
        workflow exists to prevent elsewhere."""
        guard = _step(steps, "Fail if every publish failed")
        assert "exit 1" in guard["run"]
        assert "steps.offline.outcome == 'failure'" in guard["if"]

    def test_the_summary_runs_even_when_a_publish_failed(self, steps):
        assert _step(steps, "Summarize monitored surfaces")["if"] == "always()"


class TestGuards:
    def test_skips_cleanly_without_wif(self, workflow):
        """Forks and unset repos must skip, not fail."""
        condition = workflow["jobs"]["publish"]["if"]
        assert "vars.WIF_PROVIDER" in condition
        assert "vars.AGENT_ENGINE_ID" in condition

    def test_requests_the_oidc_token(self, workflow):
        assert workflow["jobs"]["publish"]["permissions"]["id-token"] == "write"

    def test_every_engine_reference_uses_the_repo_var(self, steps):
        """A hardcoded engine id would publish another engine's scores into this
        engine's series — silently, and forever."""
        text = "\n".join(s.get("run", "") for s in steps)
        assert "vars.AGENT_ENGINE_ID" in text
        # No bare 19-digit reasoning-engine ids pasted in.
        import re

        assert not re.search(r"\b\d{19}\b", text)


class TestConfigCheckRunsFirst:
    def test_engine_config_is_verified_before_publishing(self, steps):
        """Scores from a misconfigured engine (e.g. back on 4Gi, dropping turns)
        would poison the baseline the alerting depends on."""
        names = [s.get("name") or "" for s in steps]
        cfg = next(i for i, n in enumerate(names) if "Verify engine config" in n)
        pub = next(i for i, n in enumerate(names) if "Publish offline quality" in n)
        assert cfg < pub


class TestRouterEfficiencyIsScheduled:
    """agent_router/* was the one monitored surface with no scheduled writer.

    Its three series only moved when someone ran the full eval by hand, so they
    held a handful of points: `verify_monitors`' rolling-baseline check never
    reached its 5-point minimum and the alert policies on
    `classifier_accuracy_pct` / `cost_savings_pct` / `classifier_latency_ms`
    watched something effectively static. Exactly the failure the faithfulness
    tests above exist to prevent, on a different surface.
    """

    def test_router_efficiency_has_its_own_publishing_step(self):
        assert _step(_load_steps(), "Publish router efficiency")

    def test_that_step_actually_publishes(self):
        run = _step(_load_steps(), "Publish router efficiency")["run"]
        assert "publish_router_efficiency" in run
        assert "--run" in run, "--from-json needs an artifact the cron does not have"
        assert "--dry-run" not in run, "a dry run would leave the series empty"

    def test_it_does_not_depend_on_an_engine_id(self):
        """Classifier-only, so it must keep publishing on an hour when the engine
        is down or the online step is skipped — those are exactly the hours when
        knowing the router still classifies correctly is worth most."""
        step = _step(_load_steps(), "Publish router efficiency")
        assert "AGENT_ENGINE_ID" not in step["run"]
        assert "if" not in step, "no condition should gate the cheapest surface"

    def test_one_surface_failing_does_not_hide_the_others(self):
        step = _step(_load_steps(), "Publish router efficiency")
        assert step.get("continue-on-error") is True

    def test_the_job_still_goes_red_when_every_surface_fails(self):
        """continue-on-error means a permanently-broken publish looks green in the
        Actions list unless the guard counts every surface."""
        guard = _step(_load_steps(), "Fail if every publish failed")["if"]
        for step_id in ("offline", "online", "faithfulness", "router"):
            assert f"steps.{step_id}." in guard, f"{step_id} missing from the fail guard"

    def test_the_summary_reports_it(self):
        run = _step(_load_steps(), "Summarize monitored surfaces")["run"]
        assert "router efficiency" in run
        assert "steps.router.outcome" in run
