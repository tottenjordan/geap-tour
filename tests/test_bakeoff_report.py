"""Offline tests for the coordinator bake-off report (pure assembly, no network).

``bakeoff_report`` fuses four already-computed inputs — per-model offline quality
rubrics, the pairwise SxS win-rate, per-model online traffic stats, and the fair
per-request cost — into one markdown report plus a one-line verdict. Everything
here is pure string/number assembly from stubbed inputs.
"""

from src.doe import bakeoff_report as br

BASELINE = "gemini-3.6-flash"
CANDIDATE = "claude-sonnet-5"


def _quality():
    # candidate beats baseline on every rubric.
    return {
        BASELINE: {"final_response_quality": 0.70, "tool_use_quality": 0.60},
        CANDIDATE: {"final_response_quality": 0.85, "tool_use_quality": 0.80},
    }


def _pairwise():
    return {"win_rate_candidate": 0.6, "win_rate_baseline": 0.3, "tie_rate": 0.1}


def _online():
    return {
        BASELINE: {"p50_latency": 1.0, "p95_latency": 2.0, "error_rate": 0.01},
        CANDIDATE: {"p50_latency": 1.5, "p95_latency": 3.0, "error_rate": 0.02},
    }


def _cost():
    return {BASELINE: 0.0002, CANDIDATE: 0.0010}  # candidate 5x pricier


class TestQualityDelta:
    def test_delta_is_candidate_minus_baseline(self):
        deltas = br.quality_deltas(_quality(), baseline=BASELINE, candidate=CANDIDATE)
        assert round(deltas["final_response_quality"], 3) == 0.15
        assert round(deltas["tool_use_quality"], 3) == 0.20

    def test_missing_metric_skipped(self):
        q = {BASELINE: {"a": 0.5}, CANDIDATE: {"b": 0.9}}  # no shared metric
        assert br.quality_deltas(q, baseline=BASELINE, candidate=CANDIDATE) == {}


class TestVerdict:
    def test_candidate_wins_quality_and_sxs_costs_more(self):
        verdict = br.build_verdict(
            _quality(),
            _pairwise(),
            _online(),
            _cost(),
            baseline=BASELINE,
            candidate=CANDIDATE,
        )
        assert CANDIDATE in verdict
        # Candidate wins the head-to-head.
        assert "60" in verdict or "60%" in verdict
        # 5x cost is called out.
        assert "5" in verdict
        # p95 latency delta (+1.0s = +1000ms) is called out.
        assert "1000" in verdict or "1.0" in verdict

    def test_baseline_wins_sxs_when_candidate_loses(self):
        pw = {"win_rate_candidate": 0.2, "win_rate_baseline": 0.7, "tie_rate": 0.1}
        verdict = br.build_verdict(
            _quality(),
            pw,
            _online(),
            _cost(),
            baseline=BASELINE,
            candidate=CANDIDATE,
        )
        assert BASELINE in verdict


class TestReport:
    def test_report_has_all_sections(self):
        md = br.build_bakeoff_report(
            _quality(),
            _pairwise(),
            _online(),
            _cost(),
            baseline=BASELINE,
            candidate=CANDIDATE,
        )
        assert "# Coordinator Model Bake-Off" in md
        assert "## Offline Quality" in md
        assert "## Pairwise" in md
        assert "## Online" in md
        assert "## Cost" in md
        assert "## Verdict" in md
        # Both model IDs appear as column headers.
        assert BASELINE in md
        assert CANDIDATE in md
        # A quality metric row and its delta render.
        assert "final_response_quality" in md
        assert "0.15" in md

    def test_report_handles_missing_online_and_cost(self):
        # Partial inputs (e.g. offline-only run) must not crash.
        md = br.build_bakeoff_report(
            _quality(),
            _pairwise(),
            {},
            {},
            baseline=BASELINE,
            candidate=CANDIDATE,
        )
        assert "## Verdict" in md
        assert "n/a" in md.lower()


class TestExtractors:
    def test_quality_from_results_frame(self):
        import pandas as pd

        df = pd.DataFrame(
            [
                {"model_backend": "gemini", "final_response_quality": 0.7, "tool_use_quality": 0.6},
                {
                    "model_backend": "claude",
                    "final_response_quality": 0.85,
                    "tool_use_quality": 0.8,
                },
            ]
        )
        q = br.quality_from_results_frame(
            df, level_to_model={"gemini": BASELINE, "claude": CANDIDATE}
        )
        assert q[BASELINE]["final_response_quality"] == 0.7
        assert q[CANDIDATE]["tool_use_quality"] == 0.8

    def test_online_from_grouped_monitors(self):
        # Shape mirrors verify_monitors --group-by model coordinator_quality/traffic.
        grouped = {
            "coordinator_quality": {
                "status": "ok",
                "group_by": "model",
                "metrics": {
                    "request_latency_p95": {
                        BASELINE: {"avg_score": 2.0},
                        CANDIDATE: {"avg_score": 3.0},
                    },
                    "error_rate": {
                        BASELINE: {"avg_score": 0.01},
                        CANDIDATE: {"avg_score": 0.02},
                    },
                },
            }
        }
        online = br.online_from_grouped_monitors(grouped)
        assert online[BASELINE]["p95_latency"] == 2.0
        assert online[CANDIDATE]["error_rate"] == 0.02
