"""Static guard: eval-run + pipeline-job creators keep RESOURCE_LABELS wired.

These call sites hit live Vertex services, so instead of a live test we assert
the label constant is referenced in each file — a cheap regression fence that
future refactors keep the solution label on eval runs and pipeline jobs.
"""

import pathlib

FILES = [
    "src/eval/batch_eval.py",
    "src/eval/cross_model_experiment.py",
    "src/eval/simulated_eval.py",
    "src/eval/multi_agent_batch_eval.py",
    "src/pipelines/submit.py",
    "src/pipelines/submit_optimize.py",
]


def test_label_call_sites_reference_resource_labels():
    for f in FILES:
        assert "RESOURCE_LABELS" in pathlib.Path(f).read_text(), f
