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


class TestAnomaliesReachTheOperator:
    """A detector nobody can see is not monitoring.

    The rolling baseline's first real catch — `cost_savings_pct` at 60.0, z=-2.27,
    with the static 50% floor clean — rendered in this summary as `base=ok`,
    because the column showed the baseline *status* ("a baseline was computed")
    rather than whether one FIRED. Nothing warned and nothing exited non-zero.
    """

    def test_the_summary_shows_the_z_score_when_one_fires(self):
        run = _step(_load_steps(), "Summarize monitored surfaces")["run"]
        assert "is_anomaly" in run, "the summary must branch on is_anomaly, not just status"
        assert "z=" in run

    def test_the_warning_flag_covers_anomalies_not_only_the_static_floor(self):
        run = _step(_load_steps(), "Summarize monitored surfaces")["run"]
        flag_line = next(ln for ln in run.splitlines() if 'flag = " ⚠"' in ln)
        assert "out_of_bounds" in flag_line and "is_anomaly" in flag_line

    def test_each_anomaly_emits_a_github_annotation(self):
        """A row in a collapsed summary table is not a notification."""
        run = _step(_load_steps(), "Summarize monitored surfaces")["run"]
        assert "::warning" in run
        assert 'data.get("anomalies")' in run

    def test_an_anomaly_warns_rather_than_failing_the_job(self):
        """Deliberate: the detector has one catch to its name and it was a
        transient. Failing on z>2 over a 5-point baseline would teach everyone to
        ignore a red X. The fail guard stays about publishing, not anomalies."""
        guard = _step(_load_steps(), "Fail if every publish failed")["if"]
        assert "anomal" not in guard.lower()

    def test_the_router_step_prints_a_diagnostic(self):
        """`cost_savings_pct` dropped to 60.0 and the run's log held only the three
        published scalars, so that point is permanently un-diagnosable. The tier
        distribution and score histogram cost nothing — they are already computed."""
        from src.eval.publish_router_efficiency import format_distribution

        out = format_distribution(
            {"per_case": [{"score": 0.1}, {"score": 0.9}, {"score": 0.9}]},
            {"per_case": [{"tier": "lite"}, {"tier": "opus"}, {"tier": "opus"}]},
        )
        assert "lite=1" in out and "opus=2" in out
        assert "0.9x2" in out

    def test_the_diagnostic_orders_tiers_cheapest_first(self):
        """Cost-ordered, so a shift toward the expensive end reads at a glance."""
        from src.eval.publish_router_efficiency import format_distribution

        out = format_distribution({}, {"per_case": [{"tier": "opus"}, {"tier": "lite"}]})
        assert out.index("lite") < out.index("opus")

    def test_the_diagnostic_survives_missing_inputs(self):
        """It runs right after a successful publish; it must never be what fails."""
        from src.eval.publish_router_efficiency import format_distribution

        assert format_distribution(None, None) == ""
        assert format_distribution({}, {"per_case": [{}]}) == ""


class TestOnlineFaithfulnessIsScheduled:
    """`agent_online_eval/tool_faithfulness` is alerted (< 3.0) and had no writer.

    Exactly the gap closed for the OFFLINE twin in #84, reproduced on the online
    family: the online step runs without `--faithfulness`, so a policy watched a
    series nothing wrote. Confirmed live before fixing — the metric had zero points
    while its three siblings had n=12.
    """

    def test_it_has_its_own_publishing_step(self):
        assert _step(_load_steps(), "Publish online tool-call faithfulness")

    def test_that_step_actually_publishes_faithfulness(self):
        run = _step(_load_steps(), "Publish online tool-call faithfulness")["run"]
        assert "--faithfulness" in run
        assert "online_monitor" in run
        assert "--dry-run" not in run, "a dry run would leave the series empty"

    def test_it_is_bounded(self):
        """The priciest judge here — a live stream_query trajectory plus a judge
        call per case. Unbounded, an hourly job would be the dominant cost."""
        run = _step(_load_steps(), "Publish online tool-call faithfulness")["run"]
        assert "--samples" in run

    def test_it_is_a_separate_step_from_online_quality(self):
        """Folding it into the quality step would couple a cheap rubric pass to the
        expensive trajectory capture, and one failing would hide the other."""
        quality = _step(_load_steps(), "Publish online quality")["run"]
        assert "--faithfulness" not in quality

    def test_the_summary_reports_it(self):
        run = _step(_load_steps(), "Summarize monitored surfaces")["run"]
        assert "steps.online_faithfulness.outcome" in run


class TestUnpublishedMetricsAreAnnounced:
    """A metric with an alert and no writer reads as healthy. Twice now."""

    def test_the_summary_warns_on_every_unpublished_metric(self):
        run = _step(_load_steps(), "Summarize monitored surfaces")["run"]
        assert 'data.get("unpublished")' in run
        assert "Alerted but unpublished" in run


class TestOrphanedEnginesAreDetectedOnSchedule:
    """`sonnet_agent` 8467456143491334144 was a deployment of this repo abandoned for
    ~3.5 months on the 4Gi default, and it was found by accident. Nothing had ever
    compared engines DEPLOYED against engines REFERENCED."""

    def test_it_has_its_own_step(self):
        assert _step(_load_steps(), "Find orphaned engines")

    def test_that_step_runs_the_detector(self):
        run = _step(_load_steps(), "Find orphaned engines")["run"]
        assert "find_orphan_engines" in run
        assert "--json" in run, "the summary needs machine-readable output to warn from"

    def test_it_needs_no_engine_id(self):
        """One list call against the control plane — so it keeps working on an hour
        when the engine itself is down, which is when a fleet check is worth most."""
        step = _step(_load_steps(), "Find orphaned engines")
        assert "AGENT_ENGINE_ID" not in step["run"]
        assert "if" not in step

    def test_it_never_deletes(self):
        """Read-only is the condition on which listing a shared project is
        acceptable at all — the objection `default_targets` raises."""
        run = _step(_load_steps(), "Find orphaned engines")["run"]
        assert "--delete" not in run and "delete" not in run.lower()

    def test_one_surface_failing_does_not_hide_the_others(self):
        assert _step(_load_steps(), "Find orphaned engines").get("continue-on-error") is True

    def test_the_job_still_goes_red_when_every_step_fails(self):
        guard = _step(_load_steps(), "Fail if every publish failed")["if"]
        assert "steps.orphans." in guard

    def test_findings_are_announced_and_summarised(self):
        run = _step(_load_steps(), "Summarize monitored surfaces")["run"]
        assert "Orphaned engine" in run
        assert "Dangling engine reference" in run
        assert "steps.orphans.outcome" in run


# ─────────────────────────────────────────────────────────────────────────────
# The eval gate. Same file because it is the same failure mode: a workflow whose
# broken state is invisible from its run list.
# ─────────────────────────────────────────────────────────────────────────────

EVAL_GATE = _REPO_ROOT / ".github/workflows/eval_gate.yaml"


def _gate():
    return yaml.safe_load(EVAL_GATE.read_text())


def _gate_steps():
    return _gate()["jobs"]["eval"]["steps"]


class TestTheEvalGateActuallyRuns:
    """It shipped 2026-08-14 and did not execute once until 2026-09-08.

    All 15 invocations in between were `skipped` — gated on a `run-eval` label
    nobody ever applied — so "implemented" and "never once run" looked identical.
    Its multi-turn and empty-at-200 smoke steps (roadmap P2.9) had therefore never
    been proven against a live engine, and neither had the engine-config check that
    later found a 9-day-stale CI variable.
    """

    def test_it_has_a_schedule_so_it_cannot_go_unrun(self):
        triggers = _gate().get(True) or _gate().get("on")
        assert "schedule" in triggers, (
            "without a cadence this runs only when a human remembers a label — "
            "which, measured over three weeks, is never"
        )

    def test_the_job_condition_admits_the_scheduled_event(self):
        """THE trap. Adding the trigger without adding `schedule` to the job's `if`
        leaves every scheduled run `skipped` — reintroducing the exact silent no-op
        the schedule exists to end, inside the fix for it."""
        assert "'schedule'" in _gate()["jobs"]["eval"]["if"]

    def test_manual_dispatch_and_the_label_still_work(self):
        cond = _gate()["jobs"]["eval"]["if"]
        assert "workflow_dispatch" in cond
        assert "run-eval" in cond

    def test_the_smoke_steps_are_still_wired(self):
        """P2.9's two checks: the single-turn rubric path can see neither."""
        names = [s.get("name") or "" for s in _gate_steps()]
        assert any("Multi-turn smoke" in n for n in names)
        assert any("Empty-stream" in n for n in names)


class TestTheEvalGateReportsItsOwnBreakage:
    def test_both_smoke_steps_report_outcome_not_conclusion(self):
        """`continue-on-error` rewrites `conclusion` to success; only `outcome` is
        true. Reading the wrong one has already produced a false all-green here."""
        run = _step(_gate_steps(), "Publish smoke results")["run"]
        assert "steps.multiturn.outcome" in run
        assert "steps.online_smoke.outcome" in run
        assert ".conclusion" not in run

    def test_a_wholly_broken_smoke_harness_fails_the_job(self):
        """Otherwise a permanently broken step is one word in a table nobody opens."""
        guard = _step(_gate_steps(), "Fail if every smoke check failed")["if"]
        assert "steps.multiturn.outcome == 'failure'" in guard
        assert "steps.online_smoke.outcome == 'failure'" in guard

    def test_one_flaky_smoke_check_does_not_fail_the_job(self):
        """`&&`, not `||`: a single failure is a flaky live engine, and an advisory
        gate that reds on that gets ignored."""
        guard = _step(_gate_steps(), "Fail if every smoke check failed")["if"]
        assert "&&" in guard
        assert "||" not in guard

    def test_the_score_summary_still_publishes_after_the_guard(self):
        """The guard exits 1 before it, so it must be `always()` or a failing smoke
        harness would also hide the rubric scores."""
        steps = _gate_steps()
        names = [s.get("name") or "" for s in steps]
        guard_i = next(i for i, n in enumerate(names) if "Fail if every smoke" in n)
        score_i = next(i for i, n in enumerate(names) if "Publish score" in n)
        assert score_i > guard_i
        assert "always()" in steps[score_i]["if"]
