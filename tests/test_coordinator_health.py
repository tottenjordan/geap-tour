"""Liveness and reliability are different questions, and the gate needed both.

`demo_readiness.check_engine_live` retries three times and passes on the first
success. That is correct for a *wedged* engine. At the empty rates measured
2026-09-18 — 8-19% on both live coordinator engines — it passes **99.3%** of the
time, while a 10-turn demo has an **~88%** chance of at least one blank response.

So the readiness gate reported green on an engine that would visibly fail on stage.
These tests defend the check that closes that gap, and the arithmetic that makes it
necessary.
"""

from __future__ import annotations

import pytest

from src.eval import verify_coordinator_health as vch


def _events(text: str) -> list[dict]:
    return [{"content": {"parts": [{"text": text}], "role": "model"}}] if text else []


class _FakeEngine:
    """Returns empty for the first `n_empty` turns, then real text."""

    def __init__(self, outcomes: list[str]) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.sessions = 0

    def session(self, resource, user_id, **_kw):
        self.sessions += 1
        return f"s{self.sessions}"

    def stream(self, resource, **_kw):
        i = self.calls
        self.calls += 1
        text = self.outcomes[i] if i < len(self.outcomes) else "a real answer"
        return _events(text)


def _run(outcomes, *, threshold=0.05, repeat=1, probes=None):
    engine = _FakeEngine(outcomes)
    report = vch.check_health(
        "projects/p/locations/l/reasoningEngines/1",
        repeat=repeat,
        threshold=threshold,
        probes=probes if probes is not None else [("search", "p1"), ("booking", "p2")],
        verbose=False,
        stream_fn=engine.stream,
        session_fn=engine.session,
        sleep=lambda _s: None,
    )
    return report, engine


class TestItMeasuresARateNotASingleSuccess:
    def test_a_flaky_engine_fails_even_though_some_turns_succeed(self):
        """THE property. `check_engine_live` passes here — one success is enough for
        it. This must not: half the turns came back blank."""
        report, _ = _run(["", "ok"])
        assert report["summary"]["empty_rate"] == 0.5
        assert report["verdict"]["passed"] is False

    def test_a_clean_but_tiny_run_is_inconclusive_not_a_pass(self):
        """Two clean samples cannot establish a 5% ceiling: wilson_ci(0, 2) upper is
        ~66%. Calling that PASS is how a gate certifies an engine it never
        measured. It does not red the gate either — just declines to bless it."""
        report, _ = _run(["ok", "ok"])
        assert report["summary"]["empty_rate"] == 0.0
        assert report["verdict"]["status"] == "INCONCLUSIVE"
        assert report["verdict"]["passed"] is True

    def test_enough_clean_samples_do_pass(self):
        probes = [(f"cap{i}", f"p{i}") for i in range(40)]
        report, _ = _run(["ok"] * 200, repeat=5, probes=probes)
        assert report["summary"]["n"] == 200
        assert report["verdict"]["status"] == "PASS"

    def test_zero_samples_never_passes(self):
        """An empty run is an absent measurement, not a clean bill of health — the
        same rule `stats.all_metrics_passed` enforces for eval verdicts."""
        report, _ = _run([], probes=[])
        assert report["summary"]["n"] == 0
        assert report["verdict"]["passed"] is False

    def test_the_rate_carries_a_confidence_interval(self):
        """'1 empty in 8' is not 12.5% in any useful sense at demo sample sizes."""
        report, _ = _run(["", "ok"])
        lo, hi = report["summary"]["empty_rate_ci"]
        assert lo < report["summary"]["empty_rate"] < hi

    def test_a_fresh_session_per_turn(self):
        """A reused session would let one poisoned context explain later empties,
        turning an engine problem into an apparent conversation problem."""
        _, engine = _run(["ok", "ok", "ok", "ok"], repeat=2)
        assert engine.sessions == 4


class TestTheVerdictIsThreeValued:
    """A binary verdict on a noisy rate is a coin flip, and a flapping gate is
    muted. The live run read 1/16 = 6.2% with a 95% CI of [1.1%, 28.3%] — an
    interval straddling the 5% ceiling, from which both PASS and FAIL were
    defensible. Same three-valued shape `calibration` already uses.
    """

    def test_an_interval_entirely_above_the_ceiling_fails(self):
        r = vch.three_valued_verdict(
            {"n": 200, "empty_rate": 0.25, "empty_rate_ci": (0.19, 0.32)}, threshold=0.05
        )
        assert (r["status"], r["passed"]) == ("FAIL", False)

    def test_an_interval_entirely_below_the_ceiling_passes(self):
        r = vch.three_valued_verdict(
            {"n": 200, "empty_rate": 0.01, "empty_rate_ci": (0.0, 0.04)}, threshold=0.05
        )
        assert (r["status"], r["passed"]) == ("PASS", True)

    def test_a_straddling_interval_is_inconclusive_and_does_not_red_the_gate(self):
        """THE property. The real 1/16 reading. Failing here would red the gate on
        noise; passing silently would hide a real risk. Saying 'I don't know, run N
        more' is the only honest third option."""
        r = vch.three_valued_verdict(
            {"n": 16, "empty_rate": 0.062, "empty_rate_ci": (0.011, 0.283)}, threshold=0.05
        )
        assert r["status"] == "INCONCLUSIVE"
        assert r["passed"] is True, "an inconclusive result must not fail the gate"

    def test_inconclusive_names_the_sample_size_needed(self):
        """Without it the operator learns nothing actionable."""
        r = vch.three_valued_verdict(
            {"n": 16, "empty_rate": 0.062, "empty_rate_ci": (0.011, 0.283)}, threshold=0.05
        )
        assert "turns would settle it" in r["reason"]

    def test_zero_samples_is_a_failure_not_an_inconclusive(self):
        """An absent measurement is not uncertainty about a measurement."""
        r = vch.three_valued_verdict(
            {"n": 0, "empty_rate": 0.0, "empty_rate_ci": (0.0, 0.0)}, threshold=0.05
        )
        assert (r["status"], r["passed"]) == ("FAIL", False)

    def test_the_sample_size_estimate_is_rounded_not_falsely_precise(self):
        """Its input is itself a noisy estimate; '~31 turns' would overstate it."""
        assert "~" in vch._n_needed(0.062, 0.05)
        assert vch._n_needed(0.05, 0.05).startswith("the observed rate sits")


class TestItSaysWhichCapabilityIsFlaky:
    def test_results_are_grouped_by_capability(self):
        """A flaky booking path and a flaky memory path are different problems."""
        report, _ = _run(["", "ok"], probes=[("booking", "p1"), ("search", "p2")])
        by = report["summary"]["by_tier"]
        assert by["booking"]["empty_rate"] == 1.0
        assert by["search"]["empty_rate"] == 0.0

    def test_the_probe_set_covers_every_coordinator_capability(self):
        """The coordinator holds three MCP toolsets plus Memory Bank. A probe set
        missing one cannot report that one as flaky."""
        labels = {label for label, _ in vch.PROBES}
        assert {"search", "booking", "expense", "memory"} <= labels

    def test_the_report_names_the_failing_capability(self):
        report, _ = _run(["", "ok"], probes=[("booking", "p1"), ("search", "p2")])
        text = vch.format_report(report)
        assert "by capability" in text
        assert "booking" in text


class TestTheReportIsActionable:
    def test_a_failure_explains_the_demo_consequence(self):
        """A bare 'FAIL: 50%' invites someone to demo anyway."""
        report, _ = _run(["", "ok"])
        text = vch.format_report(report)
        assert "FAIL" in text
        assert "88%" in text or "visibly fail" in text

    def test_a_non_failing_result_does_not_lecture(self):
        """The demo warning belongs on a FAIL only. Printing it on every run trains
        people to skip the last paragraph, which is where the warning lives."""
        report, _ = _run(["ok", "ok"])
        text = vch.format_report(report)
        assert report["verdict"]["status"] != "FAIL"
        assert "visibly fail" not in text

    def test_the_exit_code_follows_the_verdict(self, monkeypatch):
        engine = _FakeEngine(["", ""])
        monkeypatch.setattr(
            vch,
            "check_health",
            lambda *a, **k: {
                "summary": {
                    "n": 2,
                    "silent_empty": 2,
                    "empty_rate": 1.0,
                    "empty_rate_ci": (0.3, 1.0),
                    "full_rate": 0.0,
                    "labelled_failure": 0,
                    "counts": {"FULL": 0, "EMPTY": 2, "EMPTY_LABELLED": 0, "THROTTLED": 0},
                    "p50_latency_s": 0.0,
                    "p95_latency_s": 0.0,
                    "skipped": 0,
                    "by_tier": {},
                },
                "verdict": {
                    "passed": False,
                    "status": "FAIL",
                    "threshold": 0.05,
                    "reason": "bad",
                },
                "results": [],
                "engine": "x",
            },
        )
        assert vch.main(["--agent-id", "1"]) == 1
        assert engine.calls == 0  # the stub replaced the real probing


class TestItReusesTheRouterMachinery:
    """Three copies of a verdict rule is how two of them stayed broken (see
    `stats.all_metrics_passed`). This one imports rather than copies."""

    def test_the_shared_machinery_is_imported_not_copied(self):
        from src.eval import verify_router_health as vrh

        assert vch.summarize is vrh.summarize
        assert vch.run_probes is vrh.run_probes

    def test_the_verdict_is_deliberately_not_shared(self):
        """The one thing that is NOT reused, and on purpose. The router's verdict
        compares a point estimate to a ceiling; this module needs the three-valued
        form because a readiness run has ~8-16 samples and that interval is wide.
        Changing the router's verdict to match would alter a check that is working
        at its own sample sizes."""
        from src.eval import verify_router_health as vrh

        assert not hasattr(vch, "verdict"), "importing the binary verdict would flap"
        binary = vrh.verdict({"n": 16, "empty_rate": 0.062}, threshold=0.05)
        three = vch.three_valued_verdict(
            {"n": 16, "empty_rate": 0.062, "empty_rate_ci": (0.011, 0.283)}, threshold=0.05
        )
        assert binary["passed"] is False
        assert three["passed"] is True, "same data, and only one of them flaps"

    def test_the_threshold_matches_the_router(self):
        from src.eval.verify_router_health import DEFAULT_THRESHOLD

        assert vch.COORDINATOR_THRESHOLD == DEFAULT_THRESHOLD


class TestItIsCriticalInDemoReadiness:
    def test_the_check_is_registered_and_critical(self):
        from src.eval.demo_readiness import build_default_checks

        checks = {c["name"]: c for c in build_default_checks(engine_id="x", user_id="u")}
        assert "engine_flakiness" in checks, "the gate cannot see a flaky engine without it"
        assert checks["engine_flakiness"]["critical"] is True

    def test_it_is_distinct_from_the_liveness_check(self):
        """Both must exist: one catches a wedged engine, the other a flaky one."""
        from src.eval.demo_readiness import build_default_checks

        names = [c["name"] for c in build_default_checks(engine_id="x", user_id="u")]
        assert "engine_live" in names
        assert "engine_flakiness" in names

    def test_a_flaky_engine_turns_the_gate_red(self):
        from src.eval.demo_readiness import check_engine_flakiness

        ok, detail = check_engine_flakiness(
            engine_id="x",
            check_fn=lambda *a, **k: {
                "summary": {
                    "n": 8,
                    "silent_empty": 2,
                    "empty_rate": 0.25,
                    "empty_rate_ci": (0.07, 0.59),
                },
                "verdict": {"passed": False, "threshold": 0.05, "reason": "too high"},
            },
        )
        assert ok is False
        assert "2/8" in detail and "25%" in detail

    def test_the_detail_reports_the_interval_not_just_the_point(self):
        """At n=8 the point estimate alone invites a decision the data cannot
        support — the same lesson as the n=3 metric-noise retraction."""
        from src.eval.demo_readiness import check_engine_flakiness

        _, detail = check_engine_flakiness(
            engine_id="x",
            check_fn=lambda *a, **k: {
                "summary": {
                    "n": 8,
                    "silent_empty": 1,
                    "empty_rate": 0.125,
                    "empty_rate_ci": (0.02, 0.47),
                },
                "verdict": {"passed": False, "threshold": 0.05, "reason": "r"},
            },
        )
        assert "95% CI" in detail


class TestTheArithmeticThatMotivatesThis:
    @pytest.mark.parametrize(("turns", "at_least"), [(5, 0.60), (10, 0.85), (20, 0.98)])
    def test_a_19_percent_rate_ruins_a_demo(self, turns, at_least):
        """Why a pass-on-first-success check is not enough."""
        assert 1 - 0.81**turns > at_least

    def test_a_three_attempt_liveness_check_barely_notices(self):
        """0.19**3 — the old gate passes 99.3% of the time at a 19% empty rate."""
        assert 1 - 0.19**3 > 0.99
