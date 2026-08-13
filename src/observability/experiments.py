"""Dormant Vertex AI Experiments logging helper.

A thin, best-effort wrapper over ``google.cloud.aiplatform`` that records one
experiment *run* (params + summary metrics) so a bake-off's backbones can be
compared side-by-side in the Vertex AI console ("Experiments → Compare runs").

Design notes:
* **Dormant by default.** ``log_run`` is a clean no-op (returns ``False``,
  touches no SDK) unless an ``experiment`` name is provided — so importing this
  module or calling it in a dry run creates no billable resource.
* **Summary metrics only.** ``aiplatform.log_metrics`` records run-level scalars
  and needs **no** Managed TensorBoard (that is only required for time-series).
  Time-series logging is deliberately out of scope here.
* **Separation of concerns is the caller's.** Callers pass
  ``experiment="coordinator-bakeoff"`` vs ``"router-efficiency"``; this module
  never mixes the two — it just logs to whatever experiment it is handed.
* **Best-effort.** Any backend error (no credentials, API disabled) is swallowed
  and reported as ``False`` — logging a comparison record must never break an
  eval run.
* The ``aiplatform`` module is injectable so offline tests need no GCP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping


def _numeric_metrics(metrics: Mapping[str, Any]) -> dict[str, float]:
    """Keep only numeric (non-bool) metric values, coerced to ``float``.

    Vertex ``log_metrics`` accepts scalar floats; string sentinels like an
    ``"n/a"`` cost are dropped rather than crashing the whole run.
    """
    out: dict[str, float] = {}
    for key, value in (metrics or {}).items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            out[key] = float(value)
    return out


def log_run(
    *,
    experiment: str | None,
    run: str,
    params: Mapping[str, Any],
    metrics: Mapping[str, Any],
    aiplatform: Any = None,
) -> bool:
    """Log one experiment run (params + summary metrics); best-effort.

    Returns ``True`` if the run was logged, ``False`` on a clean no-op (no
    ``experiment`` name) or on any swallowed backend error. ``aiplatform`` is
    injectable for testing; it defaults to the real ``google.cloud.aiplatform``.
    """
    if not experiment or not experiment.strip():
        return False

    if aiplatform is None:
        from google.cloud import aiplatform as _aiplatform

        aiplatform = _aiplatform

    try:
        aiplatform.init(experiment=experiment)
        with aiplatform.start_run(run):
            if params:
                aiplatform.log_params(dict(params))
            numeric = _numeric_metrics(metrics)
            if numeric:
                aiplatform.log_metrics(numeric)
    except Exception:
        return False
    return True
