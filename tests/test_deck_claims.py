"""The workshop deck makes factual claims to an audience. Check the checkable ones.

`docs/slides.pptx` is what a presenter opens. It had gone a month without being
generated — because `python-pptx` was not in any dependency group, so
`scripts/generate_pptx.py` could not run at all in this environment. Nothing read it,
nothing tested it, and it accumulated six false statements:

* 16 slides pointed at `github.com/jswortz/geap-tour`. This repo is
  `tottenjordan/geap-tour`. An audience typing the URL off the title slide lands
  somewhere else.
* "Travel Agent — deployed + evaluated on its own" (and the same for expense).
  Neither is deployed; `deploy_agents.AGENT_SETS` has no entry for them.
* "Online Monitors — via Cloud Trace telemetry on 10-min cycles". The shipped
  monitor is client-side off `stream_query`, on an hourly cron. Wrong mechanism,
  wrong cadence.
* "Automated eval gate on PRs — score >= 3.0 to merge, blocks otherwise" and
  "Fail — PR blocked". The gate is `required: false` and the workflow says in its
  own header that it "does not block merge".
* "Cost savings: 60-80%" where the measured, monitored figure is 93.1%.
* A `generate_loss_clusters(src=...)` snippet; the parameter is `eval_result=`.

These are checked against the GENERATOR SOURCE rather than the rendered file, so the
test needs no `python-pptx` and runs everywhere. The rendered deck is verified by
regenerating it (4s) — see the module docstring of `scripts/generate_pptx.py`.

The deck itself is no longer committed — it is rebuilt in ~4s and CI uploads it as
an artifact (.github/workflows/deck.yaml) — so this guard is the only thing standing
between a false claim and a presenter's screen.

A deck is not code and most of it cannot be tested. What is tested here is the small
set of claims that have a machine-checkable counterpart in this repo.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
from typing import ClassVar

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_GENERATOR = _REPO / "scripts" / "generate_pptx.py"


@pytest.fixture(scope="module")
def deck_source() -> str:
    return _GENERATOR.read_text()


class TestTheDeckPointsAtThisRepo:
    def test_the_repo_url_matches_the_actual_remote(self, deck_source):
        """The title slide and 15 others render REPO_URL. It named a different repo."""
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=_REPO,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not remote:
            pytest.skip("no git remote configured")
        owner_repo = re.sub(r"^.*github\.com[:/]|\.git$", "", remote)
        assert owner_repo, remote
        assert owner_repo in deck_source, (
            f"the deck does not mention this repo ({owner_repo}); "
            f"a presenter's audience cannot find the code"
        )

    def test_no_other_github_owner_is_referenced(self, deck_source):
        owners = set(re.findall(r"github\.com/([A-Za-z0-9_.-]+)/geap-tour", deck_source))
        assert len(owners) <= 1, f"the deck references several repos: {sorted(owners)}"


class TestTheDeckDoesNotRepeatKnownFalseClaims:
    """Each phrase below was IN the deck and is contradicted by this repo."""

    FORBIDDEN: ClassVar[list[tuple[str, str]]] = [
        ("deployed + evaluated on its own", "travel/expense have no deployment of their own"),
        ("10-min cycles", "the online monitor runs hourly, not every 10 minutes"),
        ("Cloud Trace telemetry on", "the online monitor is client-side off stream_query"),
        ("blocks otherwise", "the eval gate is advisory; it does not block merges"),
        ("PR blocked", "the eval gate is advisory; it does not block merges"),
        ("60-80%", "cost savings are measured at 93.1%, not a guessed range"),
        ("src=eval_result_name", "generate_loss_clusters takes eval_result=, not src="),
    ]

    @pytest.mark.parametrize(("phrase", "why"), FORBIDDEN)
    def test_the_claim_is_gone(self, deck_source, phrase, why):
        assert phrase not in deck_source, f"deck claim {phrase!r} is false — {why}"


class TestTheDeckIsGenerable:
    """The root cause: it could not be built, so nobody looked at it.

    `python-pptx` now lives in the `deck` dependency group. This asserts the import
    the generator needs is declared somewhere — without importing it, so the test
    passes in an environment that skipped optional groups.
    """

    def test_python_pptx_is_a_declared_dependency(self):
        import tomllib

        cfg = tomllib.loads((_REPO / "pyproject.toml").read_text())
        declared = {
            dep
            for group in cfg.get("dependency-groups", {}).values()
            for dep in group
            if isinstance(dep, str)
        } | set(cfg["project"].get("dependencies", []))
        assert any("python-pptx" in d for d in declared), (
            "scripts/generate_pptx.py imports pptx but nothing declares it — "
            "that is why the deck went a month without being generated"
        )

    def test_the_generator_still_imports_what_it_declares(self, deck_source):
        assert "from pptx import Presentation" in deck_source


class TestTheEvalGateClaimMatchesTheWorkflow:
    """The most consequential correction: the deck promised a blocking quality gate.

    Rather than only forbidding the old wording, tie the claim to the workflow — if
    the gate ever BECOMES required, this test fails and the deck should be updated to
    say so.
    """

    def test_the_gate_is_still_advisory(self):
        wf = (_REPO / ".github/workflows/eval_gate.yaml").read_text()
        assert "advisory" in wf.lower(), (
            "eval_gate.yaml no longer describes itself as advisory — if it is now a "
            "required check, the deck may once again say it blocks merges"
        )
