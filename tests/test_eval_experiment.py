"""Offline tests for the eval-run identity + experiment-grouping helper."""

import re
from types import SimpleNamespace

import src.eval.eval_experiment as ee


# --------------------------------------------------------------------------- #
# Pure identity helpers
# --------------------------------------------------------------------------- #
def test_display_name_includes_experiment_agent_and_kind():
    dn = ee.eval_run_display_name("coordinator_agent", "batch")
    assert ee.EVAL_EXPERIMENT_NAME in dn
    assert "coordinator_agent" in dn
    assert "batch" in dn


def test_labels_carry_solution_and_grouping_keys():
    labels = ee.eval_run_labels("coordinator_agent", "cross_model_pro")
    assert labels["solution"] == "geap-tour"
    assert labels["experiment"] == "geap-batch-eval"
    assert labels["eval_agent"] == "coordinator_agent"
    assert labels["eval_kind"] == "cross_model_pro"


def test_labels_are_valid_gcp_label_values():
    labels = ee.eval_run_labels("Coordinator Agent!", "Batch Eval")
    for value in labels.values():
        assert re.fullmatch(r"[a-z0-9_-]{1,63}", value), value


def test_sanitize_label_coerces_invalid_chars_and_case():
    assert ee._sanitize_label("Coordinator Agent!") == "coordinator-agent-"
    assert ee._sanitize_label("") == "unknown"


# --------------------------------------------------------------------------- #
# ensure_eval_experiment — guarded create-or-get
# --------------------------------------------------------------------------- #
class _FakeEvals:
    def __init__(self, existing=None, created_name="projects/p/locations/l/evaluationExperiments/new"):
        self._existing = existing or []
        self._created_name = created_name
        self.create_calls = 0

    def list_evaluation_experiments(self, *, config=None):
        # Mirror the real response wrapper: a page under .evaluation_experiments
        # plus a next_page_token (single page here).
        return SimpleNamespace(evaluation_experiments=list(self._existing), next_page_token=None)

    def create_evaluation_experiment(self, **kwargs):
        self.create_calls += 1
        return SimpleNamespace(name=self._created_name, **kwargs)


class _FakeClient:
    def __init__(self, evals):
        self.evals = evals


def test_ensure_experiment_reuses_existing_by_display_name():
    existing = SimpleNamespace(
        display_name=ee.EVAL_EXPERIMENT_NAME,
        name="projects/p/locations/l/evaluationExperiments/existing",
    )
    evals = _FakeEvals(existing=[existing])
    client = _FakeClient(evals)

    name = ee.ensure_eval_experiment(client=client)

    assert name == "projects/p/locations/l/evaluationExperiments/existing"
    assert evals.create_calls == 0  # reused, not recreated


def test_ensure_experiment_creates_when_absent():
    evals = _FakeEvals(existing=[])
    client = _FakeClient(evals)

    name = ee.ensure_eval_experiment(client=client)

    assert name == "projects/p/locations/l/evaluationExperiments/new"
    assert evals.create_calls == 1


def test_ensure_experiment_matches_across_pages():
    # Match lives on the 2nd page — helper must follow next_page_token, not just
    # read the first page (regression: reuse silently created duplicates).
    match = SimpleNamespace(
        display_name=ee.EVAL_EXPERIMENT_NAME,
        name="projects/p/locations/l/evaluationExperiments/onpage2",
    )
    other = SimpleNamespace(display_name="something-else", name="projects/p/.../other")

    class _PagedEvals:
        def __init__(self):
            self.create_calls = 0

        def list_evaluation_experiments(self, *, config=None):
            if not config or not config.get("page_token"):
                return SimpleNamespace(evaluation_experiments=[other], next_page_token="tok2")
            return SimpleNamespace(evaluation_experiments=[match], next_page_token=None)

        def create_evaluation_experiment(self, **kwargs):
            self.create_calls += 1
            return SimpleNamespace(name="should-not-happen")

    evals = _PagedEvals()
    assert ee.ensure_eval_experiment(client=_FakeClient(evals)) == match.name
    assert evals.create_calls == 0


def test_ensure_experiment_returns_none_on_failure():
    class _Boom:
        @property
        def evals(self):
            raise RuntimeError("evals experiment surface not enabled")

    assert ee.ensure_eval_experiment(client=_Boom()) is None


# --------------------------------------------------------------------------- #
# Static guard: every offline eval path wires the identity + grouping helpers
# --------------------------------------------------------------------------- #
def test_all_offline_eval_paths_wire_experiment_helpers():
    import pathlib

    files = [
        "src/eval/batch_eval.py",
        "src/eval/multi_agent_batch_eval.py",
        "src/eval/simulated_eval.py",
        "src/eval/cross_model_experiment.py",
    ]
    for f in files:
        text = pathlib.Path(f).read_text()
        assert "eval_run_display_name(" in text, f
        assert "eval_run_labels(" in text, f
        assert "ensure_eval_experiment(" in text, f
