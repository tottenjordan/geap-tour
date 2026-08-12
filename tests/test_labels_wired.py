"""Static guard: eval-run + pipeline-job creators keep RESOURCE_LABELS wired.

These call sites hit live Vertex services, so instead of a live test we assert
the label constant is referenced in each file — a cheap regression fence that
future refactors keep the solution label on eval runs and pipeline jobs.
"""

import pathlib

# Pipeline jobs still reference RESOURCE_LABELS directly.
FILES = [
    "src/pipelines/submit.py",
    "src/pipelines/submit_optimize.py",
]

# Eval runs now label via eval_run_labels() (which folds in RESOURCE_LABELS) so
# the runs also carry the experiment-grouping labels. Accept either reference.
EVAL_FILES = [
    "src/eval/batch_eval.py",
    "src/eval/cross_model_experiment.py",
    "src/eval/simulated_eval.py",
    "src/eval/multi_agent_batch_eval.py",
]


def test_label_call_sites_reference_resource_labels():
    for f in FILES:
        assert "RESOURCE_LABELS" in pathlib.Path(f).read_text(), f


def test_eval_run_sites_label_via_helper_or_constant():
    for f in EVAL_FILES:
        text = pathlib.Path(f).read_text()
        assert "eval_run_labels(" in text or "RESOURCE_LABELS" in text, f
