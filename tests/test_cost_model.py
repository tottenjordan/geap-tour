"""Per-request cost model for the coordinator bake-off (token -> USD).

Prices a single coordinator turn on either backbone so the Gemini-vs-Claude
report can put a fair dollar figure next to each quality number. Gemini is
priced per-token; Claude on Vertex bills in GSU (Generative Scale Units) which
we convert to USD. All rates are pure constants — no network.
"""

import pytest

from src.eval import cost_model as cm


class TestRateTable:
    def test_both_backbones_have_rates(self):
        assert "gemini-3.6-flash" in cm.RATES
        assert "claude-sonnet-5" in cm.RATES

    def test_gemini_is_per_token_usd(self):
        rate = cm.RATES["gemini-3.6-flash"]
        assert rate["kind"] == "per_token"
        assert rate["input_usd_per_token"] > 0
        assert rate["output_usd_per_token"] > 0

    def test_claude_is_gsu_burndown(self):
        rate = cm.RATES["claude-sonnet-5"]
        assert rate["kind"] == "gsu"
        # Vertex partner-model GSU rule (<200k ctx): 1 GSU/input tok, 5 GSU/output tok.
        assert rate["input_gsu_per_token"] == 1
        assert rate["output_gsu_per_token"] == 5
        assert rate["usd_per_gsu"] > 0


class TestGemini37Flash:
    """The native-Gemini outage probe deploys on gemini-3.7-flash, so the cost
    model must price it (directional rate mirroring 3.6-flash) rather than
    KeyError inside collect_token_usage → _cost_from_usages.
    """

    def test_rate_present_and_per_token(self):
        assert "gemini-3.7-flash" in cm.RATES
        rate = cm.RATES["gemini-3.7-flash"]
        assert rate["kind"] == "per_token"
        assert rate["input_usd_per_token"] > 0
        assert rate["output_usd_per_token"] > 0

    def test_prices_without_keyerror(self):
        assert cm.per_request_cost_usd("gemini-3.7-flash", 1000, 500) > 0

    def test_cost_summary_shape(self):
        usages = [{"input_tokens": 1000, "output_tokens": 500}]
        summary = cm.cost_summary("gemini-3.7-flash", usages)
        assert summary["model"] == "gemini-3.7-flash"
        assert summary["n_requests"] == 1
        assert summary["mean_usd_per_request"] == pytest.approx(summary["total_usd"])


class TestListPrices:
    """Pin the concrete list prices (USD per 1M tokens) so they can't drift."""

    def test_gemini_per_million_tokens(self):
        r = cm.RATES["gemini-3.6-flash"]
        assert r["input_usd_per_token"] * 1_000_000 == pytest.approx(1.50)
        assert r["output_usd_per_token"] * 1_000_000 == pytest.approx(7.50)

    def test_claude_effective_per_million_tokens(self):
        # Claude prices in GSU; the effective per-token price is gsu*usd_per_gsu.
        # List price is $3/1M in, $15/1M out (the 1:5 ratio == the GSU ratio).
        assert cm.per_request_cost_usd("claude-sonnet-5", 1_000_000, 0) == pytest.approx(3.0)
        assert cm.per_request_cost_usd("claude-sonnet-5", 0, 1_000_000) == pytest.approx(15.0)


class TestPerRequestCost:
    def test_gemini_cost_is_linear_in_tokens(self):
        r = cm.RATES["gemini-3.6-flash"]
        cost = cm.per_request_cost_usd("gemini-3.6-flash", 1000, 500)
        expected = 1000 * r["input_usd_per_token"] + 500 * r["output_usd_per_token"]
        assert cost == pytest.approx(expected)

    def test_claude_cost_via_gsu(self):
        r = cm.RATES["claude-sonnet-5"]
        cost = cm.per_request_cost_usd("claude-sonnet-5", 1000, 500)
        gsu = 1000 * 1 + 500 * 5  # 1000 + 2500 = 3500 GSU
        assert cost == pytest.approx(gsu * r["usd_per_gsu"])

    def test_zero_tokens_zero_cost(self):
        assert cm.per_request_cost_usd("gemini-3.6-flash", 0, 0) == 0.0
        assert cm.per_request_cost_usd("claude-sonnet-5", 0, 0) == 0.0

    def test_unknown_model_raises(self):
        with pytest.raises(KeyError):
            cm.per_request_cost_usd("no-such-model", 10, 10)

    def test_negative_tokens_rejected(self):
        with pytest.raises(ValueError):
            cm.per_request_cost_usd("gemini-3.6-flash", -1, 10)


class TestAggregateCost:
    def test_aggregate_sums_per_request(self):
        usages = [
            {"input_tokens": 1000, "output_tokens": 500},
            {"input_tokens": 2000, "output_tokens": 100},
        ]
        total = cm.aggregate_cost_usd("gemini-3.6-flash", usages)
        expected = sum(
            cm.per_request_cost_usd("gemini-3.6-flash", u["input_tokens"], u["output_tokens"])
            for u in usages
        )
        assert total == pytest.approx(expected)

    def test_aggregate_reports_mean_per_request(self):
        usages = [
            {"input_tokens": 1000, "output_tokens": 0},
            {"input_tokens": 3000, "output_tokens": 0},
        ]
        summary = cm.cost_summary("gemini-3.6-flash", usages)
        assert summary["n_requests"] == 2
        assert summary["total_usd"] == pytest.approx(
            cm.aggregate_cost_usd("gemini-3.6-flash", usages)
        )
        assert summary["mean_usd_per_request"] == pytest.approx(summary["total_usd"] / 2)

    def test_cost_summary_empty_is_zero(self):
        summary = cm.cost_summary("claude-sonnet-5", [])
        assert summary["n_requests"] == 0
        assert summary["total_usd"] == 0.0
        assert summary["mean_usd_per_request"] == 0.0

    def test_aggregate_tolerates_missing_token_fields(self):
        # Usage dicts that don't surface tokens count as zero, not a crash.
        total = cm.aggregate_cost_usd("gemini-3.6-flash", [{}, {"input_tokens": 100}])
        assert total == pytest.approx(
            cm.per_request_cost_usd("gemini-3.6-flash", 100, 0)
        )
