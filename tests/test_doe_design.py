"""Design-matrix generation for src.doe.design."""

import pytest

from src.doe.design import build_design
from src.doe.factors import get_factors


def _all_factors():
    # The 4-factor resolution-IV screen these tests are written around. Pinned
    # explicitly so registry growth (e.g. adding `memory_bank`) doesn't change
    # what a "full run" of these design assertions covers.
    return get_factors(
        ["router_boundaries", "model_tier", "prompt_variant", "eval_fidelity"]
    )


def test_screening_has_eight_plus_baseline():
    points = build_design(_all_factors(), kind="screening")
    assert len(points) == 9
    assert points[-1].is_baseline
    assert points[-1].design_point == "baseline"


def test_full_has_sixteen():
    points = build_design(_all_factors(), kind="full")
    assert len(points) == 16
    assert not any(p.is_baseline for p in points)


def test_every_point_assigns_all_factors():
    names = {f.name for f in _all_factors()}
    for kind in ("screening", "full"):
        for p in build_design(_all_factors(), kind=kind):
            assert set(p.assignments) == names
            # each assigned label is a valid level of that factor
            for fname, label in p.assignments.items():
                factor = next(f for f in _all_factors() if f.name == fname)
                assert label in factor.labels


def test_design_point_ids_unique():
    for kind in ("screening", "full"):
        ids = [p.design_point for p in build_design(_all_factors(), kind=kind)]
        assert len(ids) == len(set(ids))


def test_baseline_is_all_low_levels():
    points = build_design(_all_factors(), kind="screening")
    baseline = points[-1]
    for f in _all_factors():
        assert baseline.assignments[f.name] == f.low_label


def test_screening_uses_both_levels_per_factor():
    # A resolution-IV design must exercise both levels of every factor.
    design_rows = build_design(_all_factors(), kind="screening")[:-1]  # drop baseline
    for f in _all_factors():
        seen = {p.assignments[f.name] for p in design_rows}
        assert seen == set(f.labels)


def test_fewer_factors_falls_back_to_full():
    two = get_factors(["router_boundaries", "model_tier"])
    points = build_design(two, kind="screening")
    assert len(points) == 4 + 1  # 2^2 full + baseline


def test_three_factor_screening_is_four_run_fraction():
    # The 4-run coordinator screen: 2^(3-1) resolution-III fraction + baseline
    # reference. Exactly 4 fractional runs (half of the 2^3=8 full factorial).
    three = get_factors(["model_tier", "prompt_variant", "memory_bank"])
    points = build_design(three, kind="screening")
    assert len(points) == 4 + 1
    assert points[-1].is_baseline
    frac = points[:-1]
    assert len(frac) == 4
    # Resolution III still exercises both levels of every factor.
    for f in three:
        assert {p.assignments[f.name] for p in frac} == set(f.labels)


def test_unknown_kind_raises():
    with pytest.raises(ValueError):
        build_design(_all_factors(), kind="bogus")


def test_empty_factors_raises():
    with pytest.raises(ValueError):
        build_design([], kind="screening")
