"""Shared identity + grouping for offline evaluation runs.

Every offline eval path (batch / multi-agent batch / simulated / cross-model)
creates a Vertex ``EvaluationRun`` tied to the deployed engine via
``create_evaluation_run(agent=<reasoningEngine resource>)`` — so the runs already
surface under that engine's **Evaluation** tab. This module gives them a stable
*identity* and best-effort *experiment grouping*:

* ``eval_run_display_name`` / ``eval_run_labels`` — a human-readable name plus an
  ``experiment``/``eval_agent``/``eval_kind`` label set (on top of the default
  ``RESOURCE_LABELS``) so runs are identifiable and filterable in the console.
* ``ensure_eval_experiment`` — create-or-get a standing ``EvaluationExperiment``
  of a fixed display name for the console's Experiments list.

Grouping is *best-effort by design*: the installed ``vertexai._genai`` evals SDK
exposes no ``create_evaluation_run(experiment=...)`` parameter, and
``EvaluationExperiment.evaluation_runs`` is server-populated (not settable via
create/update). So we cannot hard-attach a run to an experiment from the typed
API. We instead stamp the shared ``experiment`` label on every run and keep the
standing experiment resource alive; if the grouping surface isn't available the
helpers degrade to a no-op (``None``) rather than failing the eval.
"""

from __future__ import annotations

import os

from src.config import GCP_PROJECT_ID, GCP_REGION, RESOURCE_LABELS

# Fixed identity so all offline runs group under one experiment name. Matches the
# "geap-batch-eval" name the docs (docs/eval_operations.md) already advertise.
EVAL_EXPERIMENT_NAME = os.environ.get("EVAL_EXPERIMENT_NAME", "geap-batch-eval")


def _sanitize_label(value: str) -> str:
    """Coerce a string into a valid GCP label value.

    Label values must be lowercase and contain only letters, digits, ``-`` and
    ``_`` (max 63 chars). Any other character is replaced with ``-``.
    """
    out = "".join(c if (c.isalnum() or c in "-_") else "-" for c in value.lower())
    return out[:63] or "unknown"


def eval_run_display_name(agent_name: str, eval_kind: str) -> str:
    """Human-readable run name shown in the console's Evaluation tab."""
    return f"{EVAL_EXPERIMENT_NAME} · {agent_name} · {eval_kind}"


def eval_run_labels(agent_name: str, eval_kind: str) -> dict[str, str]:
    """Default resource labels plus experiment-grouping labels for a run."""
    return {
        **RESOURCE_LABELS,
        "experiment": _sanitize_label(EVAL_EXPERIMENT_NAME),
        "eval_agent": _sanitize_label(agent_name),
        "eval_kind": _sanitize_label(eval_kind),
    }


def ensure_eval_experiment(client=None, *, metadata: dict | None = None) -> str | None:
    """Create-or-get the standing ``EvaluationExperiment``; return its resource name.

    Best-effort and side-effect-safe: returns ``None`` (and prints a note) if the
    evals-experiment surface isn't available or any call fails, so it never breaks
    an eval run. Reuses an existing experiment matched by display name rather than
    piling up duplicates on repeated eval runs.
    """
    try:
        if client is None:
            import vertexai

            client = vertexai.Client(project=GCP_PROJECT_ID, location=GCP_REGION)

        for exp in client.evals.list_evaluation_experiments():
            if getattr(exp, "display_name", None) == EVAL_EXPERIMENT_NAME:
                return getattr(exp, "name", None)

        created = client.evals.create_evaluation_experiment(
            display_name=EVAL_EXPERIMENT_NAME,
            labels=dict(RESOURCE_LABELS),
            metadata=metadata or {"solution": RESOURCE_LABELS.get("solution", "geap-tour")},
        )
        return getattr(created, "name", None)
    except Exception as e:  # pragma: no cover - exercised via fakes in tests
        print(f"[eval-experiment] grouping skipped: {e}")
        return None
