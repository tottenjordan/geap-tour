"""Evalsets can't change without someone saying so.

The load-bearing test is :meth:`TestCommittedManifest.test_the_committed_evalsets_match`
— it runs against the real repo files, so an edit to any tracked evalset fails the
build until the manifest is refreshed and the version bumped. Everything else here
exercises the drift logic against fixtures.
"""

import json

import pytest

from src.eval import dataset_manifest as dm


@pytest.fixture
def evalset(tmp_path):
    """Write a minimal evalset and return its path."""

    def _write(prompts, name="x.evalset.json"):
        path = tmp_path / name
        path.write_text(
            json.dumps(
                {
                    "eval_cases": [
                        {"conversation": [{"user_content": {"parts": [{"text": p}]}}]}
                        for p in prompts
                    ]
                }
            )
        )
        return path

    return _write


class TestChecksum:
    def test_is_stable_across_reformatting(self, evalset, tmp_path):
        """Re-indenting or reordering keys must not churn the checksum, or nobody
        will keep the manifest current."""
        a = evalset(["one", "two"], "a.json")
        raw = json.loads(a.read_text())
        b = tmp_path / "b.json"
        b.write_text(json.dumps(raw, indent=4, sort_keys=True))
        assert dm.checksum(a) == dm.checksum(b)

    def test_changes_when_a_prompt_changes(self, evalset):
        assert dm.checksum(evalset(["one"], "a.json")) != dm.checksum(evalset(["one!"], "b.json"))

    def test_changes_when_a_case_is_added(self, evalset):
        assert dm.checksum(evalset(["one"], "a.json")) != dm.checksum(
            evalset(["one", "two"], "b.json")
        )

    def test_changes_when_cases_are_reordered(self, evalset):
        """Deliberate: for a set that claims to be frozen, a different order is a
        change worth one line of acknowledgement."""
        assert dm.checksum(evalset(["one", "two"], "a.json")) != dm.checksum(
            evalset(["two", "one"], "b.json")
        )

    def test_counts_cases(self, evalset):
        assert dm.describe(evalset(["a", "b", "c"]))["n_cases"] == 3


class TestVerify:
    def _tracked(self, monkeypatch, path, role="regression"):
        monkeypatch.setattr(dm, "TRACKED", {str(path): role})

    def test_matching_manifest_reports_no_drift(self, monkeypatch, evalset):
        path = evalset(["one"])
        self._tracked(monkeypatch, path)
        manifest = dm.build_manifest()
        assert dm.verify(manifest) == []

    def test_edited_dataset_is_drift(self, monkeypatch, evalset, tmp_path):
        path = evalset(["one"])
        self._tracked(monkeypatch, path)
        manifest = dm.build_manifest()
        path.write_text(
            json.dumps(
                {
                    "eval_cases": [
                        {"conversation": [{"user_content": {"parts": [{"text": "EDITED"}]}}]}
                    ]
                }
            )
        )
        [problem] = dm.verify(manifest)
        assert "without a version bump" in problem

    def test_untracked_dataset_is_drift(self, monkeypatch, evalset):
        path = evalset(["one"])
        self._tracked(monkeypatch, path)
        [problem] = dm.verify({"datasets": {}})
        assert "absent from the manifest" in problem

    def test_stale_manifest_entry_is_drift(self, monkeypatch, evalset):
        path = evalset(["one"])
        self._tracked(monkeypatch, path)
        manifest = dm.build_manifest()
        manifest["datasets"]["src/eval/evalsets/deleted.evalset.json"] = {"checksum": "x"}
        problems = dm.verify(manifest)
        assert any("no longer tracked" in p for p in problems)

    def test_rebuilding_preserves_declared_versions(self, monkeypatch, evalset):
        """A version is a human statement; auto-bumping would defeat the check."""
        path = evalset(["one"])
        self._tracked(monkeypatch, path)
        previous = {"datasets": {str(path): {"version": "2.4.0"}}}
        assert dm.build_manifest(previous)["datasets"][str(path)]["version"] == "2.4.0"


class TestCommittedManifest:
    def test_the_committed_evalsets_match(self):
        """THE regression guard. If this fails you edited a tracked evalset: bump
        its `version`, then run `python -m src.eval.dataset_manifest --update`."""
        assert dm.verify() == []

    def test_both_families_are_tracked(self):
        roles = set(dm.TRACKED.values())
        assert roles == {"regression", "development"}

    def test_the_offline_eval_sets_are_the_frozen_ones(self):
        """The sets the published scores are computed against must be the
        'regression' family — that is what makes those scores comparable."""
        from src.eval.dataset_integrity import EVAL_EVALSETS

        for path in EVAL_EVALSETS.values():
            assert dm.TRACKED[path] == "regression"

    def test_every_tracked_dataset_has_cases(self):
        """An evalset that silently became empty would still checksum cleanly on
        the next --update; catch the degenerate case explicitly."""
        for path in dm.TRACKED:
            assert dm.describe(path)["n_cases"] > 0, path


class TestCli:
    def test_check_exits_zero_when_clean(self, capsys):
        assert dm.main(["--check"]) == 0
        assert "OK" in capsys.readouterr().out

    def test_check_exits_nonzero_on_drift(self, monkeypatch, evalset, capsys):
        path = evalset(["one"])
        monkeypatch.setattr(dm, "TRACKED", {str(path): "regression"})
        monkeypatch.setattr(dm, "load_manifest", lambda *a, **k: {"datasets": {}})
        assert dm.main(["--check"]) == 1
        assert "Dataset drift" in capsys.readouterr().out


class TestCodeCaseListsAreTracked:
    """`ROUTER_EVAL_CASES` drives a MONITORED, ALERTING series but lived in Python,
    outside the evalset-JSON the manifest covered — so editing it silently moved
    `agent_router/routing_accuracy_pct`. CLAUDE.md warned about it; nothing enforced it."""

    def test_the_router_cases_are_checksummed(self):
        assert "python:src.eval.agent_eval_configs.ROUTER_EVAL_CASES" in dm.TRACKED

    def test_a_python_case_list_resolves_and_counts(self):
        from src.eval.agent_eval_configs import ROUTER_EVAL_CASES

        described = dm.describe("python:src.eval.agent_eval_configs.ROUTER_EVAL_CASES")
        assert described["n_cases"] == len(ROUTER_EVAL_CASES)

    def test_the_router_set_is_large_enough_to_resolve_its_own_alert(self):
        """The whole point of growing it 12 -> 40: at n=12 the Wilson interval
        spanned the 80% alert for every possible outcome, including a perfect
        score, so the metric could not distinguish healthy from failing."""
        from src.eval.agent_eval_configs import ROUTER_EVAL_CASES
        from src.eval.stats import resolves_threshold

        n = len(ROUTER_EVAL_CASES)
        healthy = round(0.925 * n)
        assert resolves_threshold(healthy, n, 0.80), f"n={n} still cannot resolve the 80% alert"

    def test_the_complexity_bands_stay_balanced(self):
        """Accuracy must not be dominated by whichever band is easiest."""
        from collections import Counter

        from src.eval.agent_eval_configs import ROUTER_EVAL_CASES

        counts = Counter(c["expected_complexity"] for c in ROUTER_EVAL_CASES)
        assert set(counts) == {"low", "medium", "high"}
        assert max(counts.values()) - min(counts.values()) <= 3

    def test_router_prompts_are_unique(self):
        from src.eval.agent_eval_configs import ROUTER_EVAL_CASES

        prompts = [c["prompt"] for c in ROUTER_EVAL_CASES]
        assert len(set(prompts)) == len(prompts)
