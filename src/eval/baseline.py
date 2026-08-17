"""Rolling-baseline z-score anomaly detection for monitored eval series.

The static floors in :mod:`src.eval.quality_alerts` answer one question: "is the
value below (or above) an absolute line?" A rolling baseline answers a
complementary one: "has the value moved anomalously far from its own recent
history?" — which catches a genuine regression that is *still inside* the static
floor (a slow drift down toward it) and confirms whether a floor breach is a real
step-change or just normal variance.

Pure stdlib (no GCP, no credentials). It is surfaced *additively* by
:func:`src.eval.verify_monitors._summarize` — a ``baseline`` block alongside the
existing static-floor ``out_of_bounds`` count, never replacing it. The live Cloud
Monitoring alert policies (which mutate real infra) are left untouched.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

# Need at least this many prior points before a baseline is trustworthy — below
# it, variance is dominated by noise and any z-score is meaningless.
MIN_BASELINE = 5

# |z| beyond this flags an anomaly. 2.0 ~ the 95th percentile of a normal, a
# conventional "notably far from recent history" line for a demo signal.
DEFAULT_Z_THRESHOLD = 2.0


def mean(values: Sequence[float]) -> float:
    """Arithmetic mean (``math.fsum`` for order-independent accuracy)."""
    return math.fsum(values) / len(values)


def stddev(values: Sequence[float]) -> float:
    """Sample standard deviation (ddof=1).

    Returns ``0.0`` for fewer than two points or a perfectly flat series — both
    cases where a spread is undefined, so callers treat it as "no variance".
    """
    n = len(values)
    if n < 2:
        return 0.0
    mu = mean(values)
    variance = math.fsum((v - mu) ** 2 for v in values) / (n - 1)
    return math.sqrt(variance)


def zscore(value: float, baseline: Sequence[float]) -> float | None:
    """Standard score of ``value`` against a ``baseline`` mean/std.

    Returns ``None`` when the baseline has no variance (std ``0``) — a z-score is
    undefined there rather than infinite.
    """
    sd = stddev(baseline)
    if sd == 0.0:
        return None
    return (value - mean(baseline)) / sd


def detect_regression(
    history: Sequence[float],
    current: float,
    *,
    direction: str = "LT",
    z_threshold: float = DEFAULT_Z_THRESHOLD,
    min_baseline: int = MIN_BASELINE,
) -> dict:
    """Flag whether ``current`` is an anomalous move away from its ``history``.

    ``direction`` mirrors the metric's alert direction so the check is one-sided
    in the direction that actually signals trouble:

    * ``"LT"`` (quality / accuracy floors) flags only a DROP (``z <= -z_threshold``).
      A spike *up* is good news, not a regression.
    * ``"GT"`` (latency / empty-rate ceilings) flags only a SPIKE
      (``z >= z_threshold``). A drop toward zero is good news.

    The result always carries a ``status``:

    * ``"insufficient_history"`` — fewer than ``min_baseline`` prior points.
    * ``"no_variance"`` — a perfectly flat baseline (z undefined).
    * ``"ok"`` — z computed; ``is_anomaly`` set per ``direction``.
    """
    n = len(history)
    if n < min_baseline:
        return {
            "status": "insufficient_history",
            "n_baseline": n,
            "min_baseline": min_baseline,
            "is_anomaly": False,
        }

    mu = mean(history)
    sd = stddev(history)
    z = zscore(current, history)
    if z is None:
        return {
            "status": "no_variance",
            "baseline_mean": round(mu, 3),
            "baseline_std": round(sd, 3),
            "n_baseline": n,
            "is_anomaly": False,
        }

    is_anomaly = z >= z_threshold if direction == "GT" else z <= -z_threshold
    return {
        "status": "ok",
        "baseline_mean": round(mu, 3),
        "baseline_std": round(sd, 3),
        "n_baseline": n,
        "z": round(z, 3),
        "z_threshold": z_threshold,
        "direction": direction,
        "is_anomaly": is_anomaly,
    }
