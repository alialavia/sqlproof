"""Fitting a complexity exponent. Pure -- synthetic points, no database.

Baseline subtraction is not a refinement. The Phase 1 spike measured a
RAW fit of 1.69 on a function that is exactly quadratic; subtracting the
fixed per-call cost gave 1.994. Without it the answer is simply wrong.
"""
from __future__ import annotations

from sqlproof.scale.fit import (
    FitResult,
    find_spill,
    fit_exponent,
    project_rows_before_timeout,
    segment_by_plan,
)
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


def test_zero_growth_work_is_exponent_zero():
    """A function that does zero work at every factor is genuinely
    O(1) -- there is nothing to take the log of, so this is a special
    case rather than a degenerate fit."""
    pts = _points({1: 0, 2: 0, 4: 0, 8: 0, 16: 0})
    fit = fit_exponent(pts, baseline=0)
    assert fit.exponent == 0.0
    assert fit.r_squared == 1.0
    assert fit.reason is None


def test_flat_nonzero_work_at_the_baseline_is_exponent_zero():
    """Flat work exactly at the baseline is also O(1): nothing grows,
    it just happens to grow from a nonzero floor."""
    pts = _points({1: 50, 2: 50, 4: 50, 8: 50, 16: 50})
    fit = fit_exponent(pts, baseline=50)
    assert fit.exponent == 0.0
    assert fit.r_squared == 1.0


def test_rising_work_under_the_baseline_is_refused_not_reported_as_constant():
    """Work that grows but never breaches an inflated baseline must
    stay refused. An inflated baseline must never make a genuinely
    growing function look constant."""
    pts = _points({1: 10, 2: 20, 4: 30, 8: 40, 16: 45})
    fit = fit_exponent(pts, baseline=100)
    assert fit.exponent is None
    assert fit.reason is not None


def test_work_straddling_the_baseline_is_refused():
    """Some points above the baseline, some below: not flat, and not
    fittable either -- refused, not zero."""
    pts = _points({1: 150, 2: 50, 4: 150, 8: 50, 16: 150})
    fit = fit_exponent(pts, baseline=100)
    assert fit.exponent is None
    assert fit.reason is not None


def test_growth_equal_to_the_baseline_at_the_first_point_is_refused_not_zero():
    """Ruling U: the old zero-growth guard only checked 'no point
    exceeds the first point's work', which a genuinely GROWING series
    can satisfy if its first point happens to equal the baseline. This
    is exactly linear after the first point -- not flat -- and must be
    refused, not reported as O(1)."""
    pts = _points({1: 100, 2: 10, 4: 20, 8: 40, 16: 80})
    fit = fit_exponent(pts, baseline=100)
    assert fit.exponent is None


def test_strictly_decreasing_work_is_refused_not_zero():
    """A strictly decreasing series also satisfies 'no point exceeds
    the first point's work' under the old guard, but it is not flat
    either."""
    pts = _points({1: 50, 2: 40, 4: 30, 8: 20, 16: 10})
    fit = fit_exponent(pts, baseline=50)
    assert fit.exponent is None


def test_spike_then_flat_is_refused_not_zero():
    """A spike at the first point followed by flat work also satisfies
    the old guard's 'no point exceeds the first point'. The series is
    not flat -- only points 2-5 are -- so it must be refused."""
    pts = _points({1: 500, 2: 30, 4: 30, 8: 30, 16: 30})
    fit = fit_exponent(pts, baseline=500)
    assert fit.exponent is None


def test_points_with_one_plan_are_a_single_segment():
    pts = _points({1: 100, 2: 200, 4: 400})
    segments, flips = segment_by_plan(pts)
    assert len(segments) == 1
    assert flips == []


def test_a_plan_change_splits_the_segments_and_records_the_flip():
    """Fitting across a plan change produces a meaningless number -- the
    curve is genuinely discontinuous there. Each regime is fitted on its
    own and the flip is reported as a finding in its own right."""

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
    pts = (
        _points({1: 10}, plan_hash="a")
        + _points({2: 20}, plan_hash="b")
        + _points({4: 40}, plan_hash="a")
    )
    segments, flips = segment_by_plan(pts)
    assert len(segments) == 3
    assert len(flips) == 2


def test_no_temp_blocks_means_no_spill():
    assert find_spill(_points({1: 10, 2: 20, 4: 40})) is None


def test_spill_point_is_the_first_factor_with_temp_blocks():
    """A spill is a CLIFF, not a curve: a sort that fits in work_mem is
    fast, and the moment it spills performance drops sharply. Reported
    separately so a fit run through it does not smear the
    discontinuity into a gentle-looking exponent."""

    pts = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=f * 10,
                   peak_memory_kb=100, temp_blocks=temp, plan_hash="a", exec_ms=1.0)
        for f, temp in [(1, 0), (2, 0), (4, 0), (8, 340), (16, 900)]
    ]
    spill = find_spill(pts)
    assert spill is not None
    assert spill.factor == 8
    assert spill.total_rows == 8000


def test_projection_extrapolates_the_final_regime():
    pts = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=f * 100,
                   peak_memory_kb=0, temp_blocks=0, plan_hash="a", exec_ms=f * 10.0)
        for f in (1, 2, 4, 8, 16)
    ]
    fit = fit_exponent(pts, baseline=0)
    band = project_rows_before_timeout(pts, fit, timeout_ms=1000, truncated=False)
    assert band is not None
    lo, hi = band
    assert lo < hi
    assert lo > 16_000  # beyond the largest measured point


def test_projection_is_refused_when_the_sweep_was_truncated():
    """Extrapolating past a range we stopped measuring for a reason is
    guessing, and a confident row count is exactly the wrong output."""

    pts = _points({1: 100, 2: 200, 4: 400, 8: 800, 16: 1600})
    fit = fit_exponent(pts, baseline=0)
    assert project_rows_before_timeout(pts, fit, 1000, truncated=True) is None


def test_projection_is_refused_without_a_usable_fit():
    pts = _points({1: 100, 2: 200, 4: 400, 8: 800, 16: 1600})
    bad = FitResult(None, 0.2, "too noisy", 1, 16, "a")
    assert project_rows_before_timeout(pts, bad, 1000, truncated=False) is None


def test_projection_returns_none_when_already_over_the_timeout():
    pts = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=f * 100,
                   peak_memory_kb=0, temp_blocks=0, plan_hash="a", exec_ms=5000.0 * f)
        for f in (1, 2, 4, 8, 16)
    ]
    fit = fit_exponent(pts, baseline=0)
    assert project_rows_before_timeout(pts, fit, timeout_ms=1000, truncated=False) is None


def test_projection_band_is_calculated_correctly():
    """Pin the exact band constants to catch regressions in the 0.6/1.6
    multipliers."""

    pts = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=f * 100,
                   peak_memory_kb=0, temp_blocks=0, plan_hash="a", exec_ms=f * 10.0)
        for f in (1, 2, 4, 8, 16)
    ]
    fit = fit_exponent(pts, baseline=0)
    band = project_rows_before_timeout(pts, fit, timeout_ms=1000, truncated=False)
    assert band is not None
    lo, hi = band
    # Exponent is 1.0, last.total_rows=16000, last.exec_ms=160, timeout=1000
    # centre = 16000 * (1000/160)^1 = 100000
    # lo = max(int(100000*0.6), 16000) = max(60000, 16000) = 60000
    # hi = int(100000*1.6) = 160000
    assert lo == 60000
    assert hi == 160000


def test_projection_lower_bound_is_clamped_to_measured_rows():
    """The lower bound cannot undercut row counts we already measured.
    Reproduce: exponent=1.0, last.total_rows=16000, last.exec_ms=900,
    timeout=1000 yields centre=17777.78, raw_lo=10666 < 16000. Clamp it."""

    pts = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=f * 100,
                   peak_memory_kb=0, temp_blocks=0, plan_hash="a", exec_ms=900.0)
        for f in (1, 2, 4, 8, 16)
    ]
    fit = FitResult(1.0, 0.99, None, 1, 16, "a")
    band = project_rows_before_timeout(pts, fit, timeout_ms=1000, truncated=False)
    assert band is not None
    lo, _hi = band
    assert lo >= pts[-1].total_rows  # lo must not contradict measured data


def test_projection_refuses_when_spill_occurs_even_if_exponent_is_good():
    """A spill does not change plan_hash so it is fitted through, producing
    R² near 1.0 and a confident but wrong projection over a cost cliff.
    Refuse extrapolation when any spill is detected."""

    # Linear work_blocks, but temp_blocks turn on at factor 8 and 16.
    pts = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=f * 100,
                   peak_memory_kb=0, temp_blocks=(200 if f >= 8 else 0),
                   plan_hash="a", exec_ms=f * 10.0)
        for f in (1, 2, 4, 8, 16)
    ]
    fit = fit_exponent(pts, baseline=0)
    assert fit.exponent is not None  # work_blocks alone fit well
    band = project_rows_before_timeout(pts, fit, timeout_ms=1000, truncated=False)
    assert band is None  # but projection is refused due to spill


def test_segment_by_plan_on_empty_sequence():
    """segment_by_plan handles empty input gracefully."""

    segments, flips = segment_by_plan([])
    assert segments == []
    assert flips == []


def test_segment_by_plan_on_single_point():
    """segment_by_plan handles a single point."""

    pts = _points({1: 100})
    segments, flips = segment_by_plan(pts)
    assert len(segments) == 1
    assert segments[0] == pts
    assert flips == []


def test_find_spill_on_empty_sequence():
    """find_spill handles empty input gracefully."""

    assert find_spill([]) is None


def test_find_spill_on_single_point_with_spill():
    """find_spill detects spill in a single point."""

    pt = ProbePoint(factor=1, total_rows=1000, work_blocks=100,
                    peak_memory_kb=0, temp_blocks=500, plan_hash="a", exec_ms=1.0)
    assert find_spill([pt]) is pt


def test_projection_returns_none_when_exponent_is_zero():
    """An exponent of zero means constant time regardless of scale, so
    extrapolation does not make sense."""

    pts = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=500,
                   peak_memory_kb=0, temp_blocks=0, plan_hash="a", exec_ms=100.0)
        for f in (1, 2, 4, 8, 16)
    ]
    fit = FitResult(0.0, 0.99, None, 1, 16, "a")
    assert project_rows_before_timeout(pts, fit, timeout_ms=1000, truncated=False) is None


def test_projection_returns_none_when_exponent_is_very_small():
    """A near-zero exponent means cost does not grow with scale, so there
    is no row count at which the function times out. Extrapolating past
    it is meaningless and risks OverflowError. Return None instead."""

    pts = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=100,
                   peak_memory_kb=0, temp_blocks=0, plan_hash="a", exec_ms=1.0)
        for f in (1, 2, 4, 8, 16)
    ]
    # This used to raise OverflowError for exponent 0.001
    fit = FitResult(0.001, 0.99, None, 1, 16, "a")
    assert project_rows_before_timeout(pts, fit, timeout_ms=1000.0, truncated=False) is None


def test_projection_returns_none_when_exponent_is_too_small_for_accuracy():
    """Even without raising, a very small exponent can produce absurdly
    large row counts (e.g., 9.6e152 for exponent 0.02). These are not
    measurements but artifacts of near-flat curves. Refuse them."""

    pts = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=100,
                   peak_memory_kb=0, temp_blocks=0, plan_hash="a", exec_ms=1.0)
        for f in (1, 2, 4, 8, 16)
    ]
    # This used to return (9.6e152, 2.56e153)
    fit = FitResult(0.02, 0.99, None, 1, 16, "a")
    assert project_rows_before_timeout(pts, fit, timeout_ms=1000.0, truncated=False) is None


def test_projection_band_unchanged_with_healthy_exponent():
    """Verify the fix for near-zero exponents does not affect healthy
    exponents near 1.0. The pinned band test must pass unchanged."""

    pts = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=f * 100,
                   peak_memory_kb=0, temp_blocks=0, plan_hash="a", exec_ms=f * 10.0)
        for f in (1, 2, 4, 8, 16)
    ]
    fit = fit_exponent(pts, baseline=0)
    band = project_rows_before_timeout(pts, fit, timeout_ms=1000, truncated=False)
    assert band is not None
    lo, hi = band
    # Exponent is 1.0, last.total_rows=16000, last.exec_ms=160, timeout=1000
    # centre = 16000 * (1000/160)^1 = 100000
    # lo = max(60000, 16000) = 60000, hi = 160000
    assert lo == 60000
    assert hi == 160000
