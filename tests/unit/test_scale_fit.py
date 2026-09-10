"""Fitting a complexity exponent. Pure -- synthetic points, no database.

Baseline subtraction is not a refinement. The Phase 1 spike measured a
RAW fit of 1.69 on a function that is exactly quadratic; subtracting the
fixed per-call cost gave 1.994. Without it the answer is simply wrong.
"""
from __future__ import annotations

from sqlproof.scale.fit import fit_exponent
from sqlproof.scale.probe import ProbePoint


def _points(work_by_factor: dict[int, int], plan_hash: str = "aaaa"):
    return [
        ProbePoint(
            factor=f, total_rows=f * 1000, work_blocks=w, peak_memory_kb=0,
            temp_blocks=0, plan_hash=plan_hash, exec_ms=1.0,
        )
        for f, w in sorted(work_by_factor.items())
    ]


def test_linear_work_fits_exponent_one():
    pts = _points({1: 100, 2: 200, 4: 400, 8: 800, 16: 1600})
    fit = fit_exponent(pts, baseline=0)
    assert abs(fit.exponent - 1.0) < 0.05
    assert fit.r_squared > 0.99


def test_quadratic_work_fits_exponent_two():
    pts = _points({1: 100, 2: 400, 4: 1600, 8: 6400, 16: 25600})
    fit = fit_exponent(pts, baseline=0)
    assert abs(fit.exponent - 2.0) < 0.05


def test_constant_work_fits_exponent_zero():
    pts = _points({1: 500, 2: 500, 4: 500, 8: 500, 16: 500})
    fit = fit_exponent(pts, baseline=0)
    assert abs(fit.exponent) < 0.05


def test_baseline_subtraction_changes_the_answer():
    """A fixed per-call overhead flattens the curve. These points are
    exactly quadratic ABOVE a constant 100 blocks of overhead."""
    pts = _points({1: 200, 2: 500, 4: 1700, 8: 6500, 16: 25700})
    raw = fit_exponent(pts, baseline=0)
    corrected = fit_exponent(pts, baseline=100)
    assert abs(corrected.exponent - 2.0) < 0.05
    assert raw.exponent < corrected.exponent - 0.1


def test_noisy_data_reports_low_r_squared_and_no_exponent():
    pts = _points({1: 100, 2: 5000, 4: 120, 8: 9000, 16: 300})
    fit = fit_exponent(pts, baseline=0)
    assert fit.exponent is None
    assert fit.reason is not None
    assert "r_squared" in fit.reason or "fit" in fit.reason.lower()


def test_too_few_points_is_refused_rather_than_fitted():
    pts = _points({1: 100, 2: 400})
    fit = fit_exponent(pts, baseline=0)
    assert fit.exponent is None
    assert "points" in fit.reason


def test_baseline_larger_than_measured_work_is_refused():
    """Subtracting more than was measured would produce log of a
    non-positive number. Refuse rather than emit nonsense."""
    pts = _points({1: 50, 2: 60, 4: 70, 8: 80, 16: 90})
    fit = fit_exponent(pts, baseline=100)
    assert fit.exponent is None
    assert fit.reason is not None


def test_points_with_one_plan_are_a_single_segment():
    from sqlproof.scale.fit import segment_by_plan

    pts = _points({1: 100, 2: 200, 4: 400})
    segments, flips = segment_by_plan(pts)
    assert len(segments) == 1
    assert flips == []


def test_a_plan_change_splits_the_segments_and_records_the_flip():
    """Fitting across a plan change produces a meaningless number -- the
    curve is genuinely discontinuous there. Each regime is fitted on its
    own and the flip is reported as a finding in its own right."""
    from sqlproof.scale.fit import segment_by_plan

    pts = _points({1: 100, 2: 200}, plan_hash="seq") + _points(
        {4: 50, 8: 60}, plan_hash="index"
    )
    segments, flips = segment_by_plan(pts)
    assert [len(s) for s in segments] == [2, 2]
    assert len(flips) == 1
    assert flips[0].at_factor == 4
    assert flips[0].from_hash == "seq"
    assert flips[0].to_hash == "index"


def test_flipping_back_and_forth_yields_a_segment_per_run():
    from sqlproof.scale.fit import segment_by_plan

    pts = (
        _points({1: 10}, plan_hash="a")
        + _points({2: 20}, plan_hash="b")
        + _points({4: 40}, plan_hash="a")
    )
    segments, flips = segment_by_plan(pts)
    assert len(segments) == 3
    assert len(flips) == 2
