"""The scheduled monitoring workflow's load-bearing details.

Workflows are the least-tested code in most repos and the easiest place for a
silent regression: nothing type-checks them, and a mistake only shows up as
metrics that quietly stop arriving — or, for the faithfulness flag, as a demo
artefact that gets erased with no error anywhere.

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


def _step(steps, needle):
    return next(s for s in steps if needle in (s.get("name") or ""))


class TestFaithfulnessFlagIsPresent:
    """THE test. `demo_readiness` treats a RED tool_faithfulness publish as a
    deliberate demo regression point; an hourly healthy republish erases it with
    no error, in a workflow nobody watches."""

    def test_the_offline_publish_passes_no_faithfulness(self, steps):
        run = _step(steps, "Publish offline quality")["run"]
        assert "publish_offline_eval" in run
        assert "--no-faithfulness" in run

    def test_the_flag_is_documented_at_the_top_of_the_file(self):
        """A future editor deleting the flag should hit the reason first."""
        header = WORKFLOW.read_text().split("on:")[0]
        assert "--no-faithfulness" in header
        assert "DO NOT REMOVE" in header.upper()


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
