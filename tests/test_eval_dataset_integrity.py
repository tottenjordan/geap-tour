"""Enforce the train/eval held-out split (no GEPA/eval prompt contamination)."""

from __future__ import annotations

import pytest

from src.eval import dataset_integrity as di
from src.eval.holdout import HOLDOUT_EVAL_IDS, holdout_prompts

AGENTS = ("coordinator", "travel", "expense", "router")


@pytest.mark.parametrize("agent", AGENTS)
def test_evalsets_parse_and_nonempty(agent: str) -> None:
    train = di.normalized_prompt_set(di.resolve(di.TRAIN_EVALSETS[agent]))
    ev = di.normalized_prompt_set(di.resolve(di.EVAL_EVALSETS[agent]))
    assert train, f"{agent} train evalset has no prompts"
    assert ev, f"{agent} eval evalset has no prompts"


@pytest.mark.parametrize("agent", AGENTS)
def test_holdout_ids_resolve_to_eval_prompts(agent: str) -> None:
    """Every declared holdout eval_id must exist in the eval-time evalset."""
    resolved = holdout_prompts(agent)
    declared = len(HOLDOUT_EVAL_IDS[agent])
    assert declared > 0, f"{agent} declares no holdout"
    assert len(resolved) == declared, (
        f"{agent}: {declared} holdout ids declared but {len(resolved)} resolved — "
        "an id is missing/renamed in the eval-time evalset"
    )


@pytest.mark.parametrize("agent", AGENTS)
def test_holdout_is_graded(agent: str) -> None:
    """Held-out prompts must be present in the eval-time (graded) evalset."""
    ev = di.normalized_prompt_set(di.resolve(di.EVAL_EVALSETS[agent]))
    assert holdout_prompts(agent) <= ev


@pytest.mark.parametrize("agent", AGENTS)
def test_holdout_disjoint_from_training(agent: str) -> None:
    """The core guard: held-out prompts must NEVER appear in the GEPA train set."""
    train = di.normalized_prompt_set(di.resolve(di.TRAIN_EVALSETS[agent]))
    leaked = holdout_prompts(agent) & train
    assert not leaked, (
        f"{agent}: {len(leaked)} held-out prompt(s) leaked into the GEPA training "
        f"evalset {di.TRAIN_EVALSETS[agent]} — remove them from training:\n"
        + "\n".join(f"  - {p[:70]}" for p in sorted(leaked))
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


class TestEvalsetCasesMatchTheSystem:
    """Training and eval cases must not teach behaviour the system forbids.

    GEPA can only learn what the cases show it. `flash_eval_set` carried a case
    literally named ``expense_over_limit_no_submit`` whose reference said "This
    expense cannot be auto-submitted" and whose expected trajectory omitted
    ``submit_expense`` — but the server *always* records an over-limit expense with
    ``status="pending_review"``. Separately, 11 submit cases expected
    ``submit_expense`` with no preceding ``check_expense_policy``, contradicting
    every prompt's "always check policy first". See
    docs/notes/gepa-sampler-case-audit.md.
    """

    import glob as _glob

    EVALSETS = sorted(
        set(_glob.glob("src/**/*.evalset.json", recursive=True))
        | set(_glob.glob("src/eval/evalsets/*.evalset.json"))
    )

    def _invocations(self):
        import json

        for path in self.EVALSETS:
            with open(path) as fh:
                data = json.load(fh)
            for case in data.get("eval_cases", []):
                for inv in case.get("conversation", []):
                    tools = [
                        t["name"]
                        for t in ((inv.get("intermediate_data") or {}).get("tool_uses") or [])
                    ]
                    response = " ".join(
                        p.get("text", "")
                        for p in ((inv.get("final_response") or {}).get("parts") or [])
                    )
                    yield path, case.get("eval_id"), tools, response

    def test_evalsets_are_discovered(self):
        assert len(self.EVALSETS) >= 9, self.EVALSETS

    def test_submissions_always_check_policy_first(self):
        offenders = [
            f"{p.split('/')[-1]}:{eid}"
            for p, eid, tools, _ in self._invocations()
            if "submit_expense" in tools and "check_expense_policy" not in tools
        ]
        assert not offenders, f"submit_expense without check_expense_policy: {offenders}"

    def test_no_case_teaches_refusing_an_over_limit_submission(self):
        """The server records over-limit expenses as pending_review — never refuses."""
        import re

        pattern = re.compile(r"(cannot|can't|won't|unable to|will not)[\s\w-]{0,20}submit", re.I)
        offenders = [
            f"{p.split('/')[-1]}:{eid}"
            for p, eid, _, response in self._invocations()
            if pattern.search(response)
        ]
        assert not offenders, f"cases teaching refuse-to-submit: {offenders}"

    def test_expected_tools_all_exist_on_a_server(self):
        from src.eval.verify_mcp_tools import EXPECTED_TOOLS

        real = {t for tools in EXPECTED_TOOLS.values() for t in tools} | {"transfer_to_agent"}
        offenders = {t for _, _, tools, _ in self._invocations() for t in tools if t not in real}
        assert not offenders, f"eval cases expect non-existent tools: {sorted(offenders)}"
