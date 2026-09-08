"""The GEPA report must not silently describe a system that no longer exists.

Its 2026-05 edition listed five engine ids in an "Agent Overview" table. By
2026-09 **every one of them was dead** — four already deleted, the fifth
(`sonnet_agent` 8467456143491334144) an unlabelled orphan with zero traffic for
30 days, on the 4Gi default that OOM-kills workers. Nothing in the document said
when it was written or that the ids were a snapshot, so a reader had no way to
tell a stale id from a live one, and the table read as current.

The models were hardcoded to the Gemini-3 defaults too, so regenerating would
have produced *correct* engine ids beside the *wrong* models — the deployed tier
engines are pinned to Gemini-2.5.

Pure: imports the generator module and reads committed markdown. No GCP, no eval.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT = _REPO_ROOT / "docs/gepa_optimization_analysis.md"


@pytest.fixture(scope="module")
def agents():
    from scripts.generate_optimization_report import AGENTS

    return AGENTS


class TestTheGeneratorCannotDriftFromConfig:
    def test_models_come_from_config_not_hardcoded_literals(self, agents):
        """A hardcoded model list drifts the moment a tier is re-pinned, and the
        report keeps asserting the old one with a straight face."""
        from src.config import FLASH_MODEL, LITE_MODEL, OPUS_MODEL, PRO_MODEL, SONNET_MODEL

        assert agents["lite_agent"]["model"] == LITE_MODEL
        assert agents["flash_agent"]["model"] == FLASH_MODEL
        assert agents["pro_agent"]["model"] == PRO_MODEL
        assert agents["sonnet_agent"]["model"] == SONNET_MODEL
        assert agents["opus_agent"]["model"] == OPUS_MODEL

    def test_prices_come_from_the_shared_cost_table(self, agents):
        """Two hand-maintained price lists diverge; the bake-off already learned it."""
        from src.router.cost_tracker import COST_RATES

        for spec in agents.values():
            rates = COST_RATES[spec["model"]]
            assert spec["output_cost"] == rates["output"]
            assert spec["input_cost"] == rates["input"]

    def test_tier_order_matches_the_router_ladder(self, agents):
        """The old table had pro at Tier 3 and sonnet at Tier 4, which is backwards:
        `score_to_model_tier` orders lite -> flash -> SONNET -> PRO -> opus. A reader
        comparing the report against routing behaviour would have found them swapped."""
        import re

        from src.router.complexity import score_to_model_tier

        by_tier = sorted(
            agents.items(), key=lambda kv: int(re.search(r"Tier (\d)", kv[1]["tier"]).group(1))
        )
        assert [k.replace("_agent", "") for k, _ in by_tier] == [
            "lite",
            "flash",
            "sonnet",
            "pro",
            "opus",
        ]
        # ...and that really is the router's order, not just a list we wrote down.
        ladder = [score_to_model_tier(s) for s in (0.1, 0.4, 0.75, 0.93, 0.99)]
        assert ladder == ["lite", "flash", "sonnet", "pro", "opus"]

    def test_the_report_is_stamped_with_a_generation_date(self):
        """Point-in-time content with no date is indistinguishable from current."""
        import inspect

        from scripts import generate_optimization_report as gen

        src = inspect.getsource(gen.generate_report)
        assert "point-in-time" in src.lower()
        assert "strftime" in src, "the generated report must carry its own date"


class TestTheCommittedReportSaysItIsHistorical:
    def test_it_warns_that_the_engine_ids_are_dead(self):
        text = REPORT.read_text()
        head = text[: text.index("## Agent Overview")]
        assert "HISTORICAL" in head
        assert "dead" in head.lower() or "deleted" in head.lower()

    def test_it_points_at_where_the_live_ids_are(self):
        head = REPORT.read_text()
        assert "verify_engine_config" in head
        assert "ENGINE_ID" in head

    def test_it_does_not_pretend_the_dead_ids_are_current(self):
        """Swapping in today's ids would attribute May's measurements to engines
        that did not exist then and now run different models."""
        text = REPORT.read_text()
        assert "8467456143491334144" in text, "the historical id stays, labelled as historical"
