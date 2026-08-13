"""Bridge evaluation scores onto ``custom.googleapis.com/agent_eval/*`` metrics.

The native Online Evaluators (``setup_online_evaluators.py``) surface their
scores in the Agent Engine console and Cloud Logging. To make those scores fire
the alert policies in ``quality_alerts.py`` and chart on the observability
dashboard, they must also land on the ``agent_eval/*`` Cloud Monitoring series.

This module is the single, canonical bridge: it takes a ``{metric_name: score}``
dict (from an evaluator run, a batch eval summary, or a verify pass), maps any
known evaluator/rubric metric names onto the canonical monitored names, drops
anything not in ``ALL_MONITORED_METRICS`` (so no metric drift), and writes the
survivors via ``MetricsWriter.write_quality_scores``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.eval.quality_alerts import ALL_MONITORED_METRICS
from src.observability.metrics import MetricsWriter, write_quality_scores

if TYPE_CHECKING:
    from collections.abc import Mapping

# Canonical set of metric names the alerts + dashboard track. No drift allowed.
MONITORED_METRIC_NAMES: set[str] = {name for name, _threshold in ALL_MONITORED_METRICS}

# Map evaluator / rubric metric names onto the canonical monitored names.
# Native predefined metrics and GEAP custom rubrics both feed the same series.
EVAL_METRIC_ALIASES: dict[str, str] = {
    "final_response_quality_v1": "helpfulness",
    "final_response_quality": "helpfulness",
    "response_quality": "helpfulness",
    "tool_use_quality_v1": "tool_use_accuracy",
    "tool_use_quality": "tool_use_accuracy",
    "tool_use": "tool_use_accuracy",
    "GEAP Policy Compliance": "policy_compliance",
    "policy": "policy_compliance",
}


def _canonical_name(name: str) -> str:
    return EVAL_METRIC_ALIASES.get(name, name)


def publish_eval_metrics(
    scores: Mapping[str, float | None],
    writer: MetricsWriter | None = None,
    extra_labels: Mapping[str, str] | None = None,
) -> dict[str, float]:
    """Publish eval ``scores`` to ``agent_eval/*`` gauges; return what was written.

    Only names that resolve (directly or via alias) to a member of
    ``ALL_MONITORED_METRICS`` are published; ``None`` scores are skipped. The
    returned dict is the exact ``{canonical_name: value}`` that was emitted.
    """
    canonical: dict[str, float] = {}
    for name, value in scores.items():
        if value is None:
            continue
        key = _canonical_name(name)
        if key in MONITORED_METRIC_NAMES:
            canonical[key] = float(value)

    if canonical:
        write_quality_scores(canonical, writer=writer, extra_labels=extra_labels)
    return canonical


if __name__ == "__main__":
    import json
    import sys

    raw = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    written = publish_eval_metrics(raw)
    print(f"Published {len(written)} agent_eval/* metric(s): {written}")
