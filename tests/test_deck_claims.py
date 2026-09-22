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


class TestTheDiagramsReachAFreshCheckout:
    """The deck embeds seven generated diagrams. CI builds from a clean clone.

    `diagrams/outputs/` was covered by an unanchored `outputs/` rule. The eight PNGs
    there survived only because they had been committed BEFORE the rule existed —
    tracked files beat `.gitignore`. A ninth would have been silently ignored, absent
    from CI's checkout, and dropped from the deck without complaint, because
    `add_image_safe` returns False and not one of its 17 call sites checks it.

    Verified at the time: `git check-ignore` matched a newly created
    `diagrams/outputs/09_test.png` against `.gitignore:66:outputs/`.
    """

    def test_every_diagram_the_deck_embeds_is_tracked(self):
        import re
        import subprocess

        src = (_REPO / "scripts" / "generate_pptx.py").read_text()
        wanted = set(re.findall(r'DIAGRAMS,\s*"([^"]+\.png)"', src))
        assert wanted, "the deck no longer embeds any diagram — has DIAGRAMS moved?"
        tracked = set(
            subprocess.run(
                ["git", "ls-files", "diagrams/outputs"],
                cwd=_REPO,
                capture_output=True,
                text=True,
            ).stdout.split()
        )
        missing = {w for w in wanted if f"diagrams/outputs/{w}" not in tracked}
        assert not missing, (
            f"the deck embeds {sorted(missing)} but git does not track them — a clean "
            "CI checkout builds a deck with blank slides"
        )

    def test_a_new_diagram_would_not_be_silently_ignored(self):
        """The actual trap. Anchoring the rule is what fixes it; this proves it stays
        fixed for a diagram that does not exist yet."""
        import subprocess

        probe = "diagrams/outputs/99_a_diagram_added_tomorrow.png"
        ignored = subprocess.run(["git", "check-ignore", "-q", probe], cwd=_REPO).returncode == 0
        assert not ignored, (
            f"{probe} would be gitignored, so a newly added diagram could never be "
            "committed and the deck would lose it silently"
        )

    def test_the_run_scaffolding_is_still_ignored(self):
        """The other half: narrowing the rule must not start committing paperbanana's
        per-run intermediates (132MB at the repo root alone)."""
        import subprocess

        for probe in ("outputs/run_x/y.png", "diagrams/outputs/batch_20260101_x/z.png"):
            ignored = (
                subprocess.run(["git", "check-ignore", "-q", probe], cwd=_REPO).returncode == 0
            )
            assert ignored, f"{probe} is no longer ignored — run scaffolding would be committed"


class TestAMissingImageIsLoud:
    def test_the_generator_reports_missing_images(self):
        src = (_REPO / "scripts" / "generate_pptx.py").read_text()
        assert "MISSING_IMAGES" in src
        assert "IMAGE(S) MISSING" in src, "the summary line CI greps for is gone"

    def test_ci_fails_on_an_incomplete_deck(self):
        wf = (_REPO / ".github/workflows/deck.yaml").read_text()
        assert "IMAGE(S) MISSING" in wf, (
            "deck.yaml no longer checks for missing images; a deck with blank slides "
            "would upload as a green artifact"
        )
