"""Generate a design matrix (which factor-level combinations to run).

Two kinds:
  - ``screening`` — a resolution-IV ``2^(4-1)`` half-fraction (8 runs) for the
    canonical 4-factor experiment, so main effects are clear of two-factor
    interactions at a quarter the cost of a full factorial. For <4 factors this
    degrades to the full factorial (nothing to fractionate). A ``baseline``
    reference point (every factor at its low/coded-``-1`` level) is appended as
    a replicate anchor, giving 8 + 1 = 9 points.
  - ``full`` — the full ``2^k`` factorial via ``ff2n`` (16 runs for k=4). No
    extra baseline point (the anchor corner is already present).

Coded ``-1`` maps to a factor's low label (labels[0]); ``+1`` to the high label
(labels[1]). Column order follows the factor list order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyDOE3 import ff2n, fracfact

from src.doe.factors import Factor

# Resolution-IV generators keyed by factor count for screening designs.
_SCREENING_GENERATORS = {
    4: "a b c abc",  # 2^(4-1), 8 runs, resolution IV
}


@dataclass(frozen=True)
class DesignPoint:
    """One configuration to run: a factor→level-label assignment."""

    design_point: str  # stable id, e.g. "dp01" or "baseline"
    assignments: dict[str, str] = field(default_factory=dict)  # factor name -> level label
    is_baseline: bool = False


def _coded_to_points(matrix, factors: list[Factor], prefix: str = "dp") -> list[DesignPoint]:
    points: list[DesignPoint] = []
    for i, row in enumerate(matrix, start=1):
        assignments = {
            f.name: (f.low_label if val < 0 else f.high_label)
            for f, val in zip(factors, row, strict=True)
        }
        points.append(DesignPoint(design_point=f"{prefix}{i:02d}", assignments=assignments))
    return points


def _baseline_point(factors: list[Factor]) -> DesignPoint:
    """Reference corner: every factor at its low (coded -1) level."""
    return DesignPoint(
        design_point="baseline",
        assignments={f.name: f.low_label for f in factors},
        is_baseline=True,
    )


def build_design(factors: list[Factor], kind: str = "screening") -> list[DesignPoint]:
    """Build the list of design points for the given factors and design kind."""
    if not factors:
        raise ValueError("build_design requires at least one factor")
    k = len(factors)

    if kind == "full":
        return _coded_to_points(ff2n(k), factors)

    if kind == "screening":
        if k in _SCREENING_GENERATORS:
            matrix = fracfact(_SCREENING_GENERATORS[k])
        elif k < 4:
            # Nothing meaningful to fractionate — use the full factorial.
            matrix = ff2n(k)
        else:
            raise NotImplementedError(
                f"screening design for {k} factors is not defined; add a "
                f"resolution generator to _SCREENING_GENERATORS or use kind='full'"
            )
        points = _coded_to_points(matrix, factors)
        points.append(_baseline_point(factors))
        return points

    raise ValueError(f"unknown design kind {kind!r}; expected 'screening' or 'full'")
