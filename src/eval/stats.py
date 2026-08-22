"""Pure statistical-rigor helpers for eval aggregates (no GCP/SDK dependency).

The eval surface historically reported bare point estimates — a mean rubric
score, a raw win-rate — with **no notion of sample size or uncertainty**. A
"0.62 win-rate" over 5 cases and over 500 cases printed identically, and an
aggregate over 3 samples was treated as trustworthy as one over 300. This module
adds the missing rigor as small, deterministic, unit-tested primitives:

* :func:`is_low_confidence` / :func:`confidence_label` — a sample-size floor so
  aggregates over too few cases are flagged rather than silently trusted.
* :func:`bootstrap_mean_ci` — a percentile bootstrap confidence interval on the
  mean of continuous scores (e.g. online-monitor rubric means). Seeded, so it is
  reproducible in tests and run-to-run.
* :func:`binomial_two_sided_p` + :func:`wilson_ci` + :func:`win_rate_significance`
  — a sign test and a proportion CI for the pairwise SxS win-rate, so "candidate
  beats baseline" is only asserted when it clears significance, not on a coin-flip
  majority over a handful of cases.

Everything here is pure Python (``math`` / ``random`` / ``statistics`` only) so it
runs in unit tests and CI with no cloud calls.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Sequence

# Below this many observations an aggregate is flagged low-confidence. Chosen to
# match the demo-scale evalsets (~8-25 cases); override per call site as needed.
MIN_SAMPLES = 8

# Bootstrap defaults — enough resamples for a stable percentile CI on small n.
DEFAULT_RESAMPLES = 2000
DEFAULT_CONFIDENCE = 0.95


def is_low_confidence(n: int, floor: int = MIN_SAMPLES) -> bool:
    """True when ``n`` observations is below the trust floor."""
    return n < floor


def confidence_label(n: int, floor: int = MIN_SAMPLES) -> str:
    """``"low_confidence"`` when ``n`` is below the floor, else ``"ok"``."""
    return "low_confidence" if is_low_confidence(n, floor) else "ok"


def _percentile(sorted_vals: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted sequence."""
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return float(sorted_vals[int(rank)])
    frac = rank - lo
    return float(sorted_vals[lo]) * (1.0 - frac) + float(sorted_vals[hi]) * frac


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    confidence: float = DEFAULT_CONFIDENCE,
    n_resamples: int = DEFAULT_RESAMPLES,
    seed: int = 0,
) -> tuple[float, float]:
    """Percentile-bootstrap CI on the mean of ``values``.

    Returns ``(low, high)``. An empty input yields ``(nan, nan)``; a single value
    yields ``(v, v)``. Deterministic for a fixed ``seed`` so tests and repeated
    runs agree.
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n == 0:
        return (float("nan"), float("nan"))
    if n == 1:
        return (vals[0], vals[0])
    rng = random.Random(seed)
    means = [sum(vals[rng.randrange(n)] for _ in range(n)) / n for _ in range(n_resamples)]
    means.sort()
    alpha = 1.0 - confidence
    lo = _percentile(means, (alpha / 2.0) * 100.0)
    hi = _percentile(means, (1.0 - alpha / 2.0) * 100.0)
    return (lo, hi)


def binomial_two_sided_p(k: int, n: int, p: float = 0.5) -> float:
    """Exact two-sided binomial p-value for ``k`` successes in ``n`` trials.

    Sums the probability of every outcome no more likely than the observed one
    (the standard two-sided exact test). ``n == 0`` returns ``1.0``.
    """
    if n <= 0:
        return 1.0
    q = 1.0 - p

    def pmf(i: int) -> float:
        return math.comb(n, i) * (p**i) * (q ** (n - i))

    observed = pmf(k)
    tol = observed * (1.0 + 1e-9)
    total = sum(pmf(i) for i in range(n + 1) if pmf(i) <= tol)
    return min(1.0, total)


def _z_for(confidence: float) -> float:
    """Two-sided z critical value for a confidence level (e.g. 0.95 -> ~1.96)."""
    return statistics.NormalDist().inv_cdf(1.0 - (1.0 - confidence) / 2.0)


def wilson_ci(k: int, n: int, *, confidence: float = DEFAULT_CONFIDENCE) -> tuple[float, float]:
    """Wilson score interval for a proportion ``k / n`` (robust at extremes).

    Returns ``(low, high)`` clamped to ``[0, 1]``; ``n == 0`` returns ``(0.0, 0.0)``.
    """
    if n <= 0:
        return (0.0, 0.0)
    z = _z_for(confidence)
    phat = k / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2.0 * n)) / denom
    margin = (z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def win_rate_significance(
    wins: int, losses: int, *, alpha: float = 0.05, confidence: float = DEFAULT_CONFIDENCE
) -> dict:
    """Sign test + Wilson CI on a pairwise win-rate (ties excluded upstream).

    ``wins``/``losses`` are the decisive counts; the denominator is their sum.
    Returns the win-rate among decisive cases, the exact two-sided p-value against
    a 50/50 null, whether it clears ``alpha``, and the proportion CI.
    """
    decisive = wins + losses
    if decisive <= 0:
        return {
            "wins": wins,
            "losses": losses,
            "decisive": 0,
            "win_rate_decisive": 0.0,
            "p_value": 1.0,
            "significant": False,
            "ci_low": 0.0,
            "ci_high": 0.0,
            "alpha": alpha,
        }
    p_value = binomial_two_sided_p(wins, decisive, 0.5)
    lo, hi = wilson_ci(wins, decisive, confidence=confidence)
    return {
        "wins": wins,
        "losses": losses,
        "decisive": decisive,
        "win_rate_decisive": wins / decisive,
        "p_value": p_value,
        "significant": p_value < alpha,
        "ci_low": lo,
        "ci_high": hi,
        "alpha": alpha,
    }


# ---------------------------------------------------------------------------
# Statistical POWER — can this sample support the threshold it is judged against?
# ---------------------------------------------------------------------------
# A sample-size floor like MIN_SAMPLES is threshold-blind: it asks "are there
# enough observations?" when the question that matters is "enough *for this
# threshold*?". Those diverge badly. `routing_accuracy_pct` is computed from 12
# router eval cases and alerts below 80%; at n=12 the Wilson interval spans 80%
# for EVERY possible outcome, including a perfect 12/12 — a perfect score cannot
# be distinguished from a failing one. `is_low_confidence(12)` is False, so the
# blanket floor passes a metric that is pure noise relative to its own alert.
#
# These helpers ask the threshold-relative question instead.

# Cap on the search in `min_n_for_threshold`. Beyond this the honest answer is
# "this threshold is not reachable at any sample size you will collect".
MAX_SEARCH_N = 10_000


def resolves_threshold(
    k: int, n: int, threshold: float, *, confidence: float = DEFAULT_CONFIDENCE
) -> bool:
    """True when ``k/n`` lands decisively on one side of ``threshold``.

    Decisive means the whole Wilson interval sits above or below it. When the
    interval *spans* the threshold, the pass/fail verdict is a coin-flip dressed
    as a measurement, and callers should say "cannot tell" rather than pick a side.
    """
    if n <= 0:
        return False
    lo, hi = wilson_ci(k, n, confidence=confidence)
    return lo > threshold or hi < threshold


def min_n_for_threshold(
    observed_rate: float, threshold: float, *, confidence: float = DEFAULT_CONFIDENCE
) -> int | None:
    """Smallest ``n`` at which ``observed_rate`` would resolve against ``threshold``.

    This is what turns "underpowered" into something actionable — not "trust this
    less" but "you need about 40 cases". Returns ``None`` when the rate sits on the
    threshold (no sample size ever separates them) or the search cap is hit.

    Searches upward rather than inverting the Wilson formula: the closed form is
    fiddly and this runs a handful of times per report, not per item.
    """
    if math.isclose(observed_rate, threshold):
        return None
    n = 1
    while n <= MAX_SEARCH_N:
        # Round to the nearest achievable success count at this n.
        if resolves_threshold(round(observed_rate * n), n, threshold, confidence=confidence):
            return n
        n += 1
    return None


def power_report(
    k: int, n: int, threshold: float, *, confidence: float = DEFAULT_CONFIDENCE
) -> dict:
    """One shape every caller can report: is this verdict supported, and if not, what would be.

    Keys: ``n``, ``rate``, ``ci`` (low, high), ``threshold``, ``resolved``,
    ``needed_n`` (``None`` when already resolved or unreachable), and ``verdict``
    — one of ``"above"`` / ``"below"`` / ``"inconclusive"``.
    """
    rate = (k / n) if n > 0 else 0.0
    lo, hi = wilson_ci(k, n, confidence=confidence)
    resolved = resolves_threshold(k, n, threshold, confidence=confidence)
    verdict = "inconclusive"
    if resolved:
        verdict = "above" if lo > threshold else "below"
    return {
        "n": n,
        "rate": rate,
        "ci": (lo, hi),
        "threshold": threshold,
        "resolved": resolved,
        "needed_n": None
        if resolved
        else min_n_for_threshold(rate, threshold, confidence=confidence),
        "verdict": verdict,
    }


# Below this a bootstrap cannot express any uncertainty: resampling one or two
# points yields a degenerate interval (n=1 gives a zero-width CI, which would read
# as a confident verdict). Treat such samples as underpowered regardless.
MIN_BOOTSTRAP_N = 3


def mean_power_report(
    scores: Sequence[float],
    threshold: float,
    comparison: str = "LT",
    *,
    confidence: float = DEFAULT_CONFIDENCE,
) -> dict:
    """Can this sample resolve whether its MEAN sits past ``threshold``?

    The proportion-based :func:`power_report` answers "what share of points are
    good", which is the wrong question for a monitored gauge: the alert fires on
    the metric's *value*, so the claim under test is about the mean. Framing it as
    a share also sets an unreachable bar — resolving a 90% good-share needs ~35
    points, so a perfectly healthy 24-point series would read as underpowered and
    every alert would be suppressed.

    Uses the percentile bootstrap CI on the mean. ``comparison`` is ``"LT"`` (the
    metric is a floor; breaching means falling below) or ``"GT"`` (a ceiling).

    Returns ``resolved``, ``ci``, ``mean``, ``n`` and ``verdict`` — one of
    ``"healthy"`` / ``"breached"`` / ``"inconclusive"``.
    """
    n = len(scores)
    if n == 0:
        return {
            "n": 0,
            "mean": float("nan"),
            "ci": (float("nan"), float("nan")),
            "threshold": threshold,
            "resolved": False,
            "verdict": "inconclusive",
        }
    lo, hi = bootstrap_mean_ci(scores, confidence=confidence)
    spans = lo <= threshold <= hi
    resolved = (not spans) and n >= MIN_BOOTSTRAP_N
    if not resolved:
        verdict = "inconclusive"
    elif comparison == "GT":
        verdict = "breached" if lo > threshold else "healthy"
    else:
        verdict = "breached" if hi < threshold else "healthy"
    return {
        "n": n,
        "mean": sum(scores) / n,
        "ci": (lo, hi),
        "threshold": threshold,
        "resolved": resolved,
        "verdict": verdict,
    }
