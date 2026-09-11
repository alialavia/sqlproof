"""The assertion surface.

Row-count figures here are TOTAL ROWS ACROSS THE PROFILE
(`sum(sizes.values()) * factor`), never one table's count -- the sweep
scales a whole profile, so a single table's count would be ambiguous.
"""
from __future__ import annotations

import pytest

from sqlproof.exceptions import SqlProofScaleError
from sqlproof.scale.fit import FitResult, PlanFlip
from sqlproof.scale.probe import ProbePoint
from sqlproof.scale.result import ScaleResult


def _pt(factor, work, temp=0, plan="a", ms=1.0):
    return ProbePoint(
        factor=factor, total_rows=factor * 1000, work_blocks=work,
        peak_memory_kb=0, temp_blocks=temp, plan_hash=plan, exec_ms=ms,
    )


def _result(**kw):
    defaults = dict(
        points=[_pt(f, f * 100) for f in (1, 2, 4, 8, 16)],
        regimes=[FitResult(1.0, 0.999, None, 1, 16, "a")],
        plan_flips=[],
        truncated=False,
        function="f",
        sizes={"t": 1000},
    )
    defaults.update(kw)
    return ScaleResult(**defaults)


def test_exponent_comes_from_the_final_regime():
    assert _result().exponent == 1.0


def test_exponent_uses_the_last_regime_not_the_first():
    """Two regimes with different exponents. `_result()`'s default
    fixture always supplies exactly one regime, so it cannot tell
    `regimes[-1]` (the current strategy) apart from `regimes[0]` (a
    strategy the planner has already abandoned) -- a `regimes[0]`
    mutant would report the pre-flip 2.0 instead of the current 1.0.
    """
    r = _result(regimes=[
        FitResult(2.0, 0.99, None, 1, 4, "a"),
        FitResult(1.0, 0.99, None, 4, 16, "a"),
    ])
    assert r.exponent == 1.0


def test_exponent_raises_when_the_fit_was_inconclusive():
    """Returning None would make `assert result.exponent < 1.5` fail
    with a TypeError that says nothing about why. Raising carries the
    reason."""
    bad = _result(regimes=[FitResult(None, 0.3, "r_squared 0.300 below 0.98", 1, 16, "a")])
    with pytest.raises(SqlProofScaleError, match=r"0.98"):
        _ = bad.exponent


def test_exponent_raises_when_every_point_flipped_plan():
    flips = [PlanFlip(2, "a", "b"), PlanFlip(4, "b", "c")]
    r = _result(regimes=[], plan_flips=flips)
    with pytest.raises(SqlProofScaleError, match="plan") as exc_info:
        _ = r.exponent
    # Pins the actual flip count into the message, not just the word
    # "plan" -- a mutant that hardcodes the count (e.g. to 0) would
    # still match "plan" but not "2 flips".
    assert "2 flips" in str(exc_info.value)


def test_r_squared_comes_from_the_final_regime():
    r = _result(regimes=[
        FitResult(2.0, 0.80, None, 1, 4, "a"),
        FitResult(1.0, 0.999, None, 4, 16, "a"),
    ])
    assert r.r_squared == 0.999


def test_r_squared_is_none_when_there_are_no_regimes():
    r = _result(regimes=[], plan_flips=[PlanFlip(2, "a", "b")])
    assert r.r_squared is None


def test_spills_below_is_false_within_the_measured_range_when_nothing_spilled():
    """The default points measure 1,000-16,000 total rows, none spilling."""
    assert _result().spills_below(8_000) is False
    assert _result().spill_point_rows is None


def test_spills_below_at_exactly_the_largest_measured_total_is_false():
    """16,000 rows WAS measured, so "no spill there" is an answer."""
    assert _result().spills_below(16_000) is False


def test_spills_below_beyond_the_measured_range_without_a_spill_raises():
    """Ruling AP: one row past the largest measured total is a row count
    nobody measured. `assert not spills_below(500_000)` used to pass
    vacuously after a sweep that stopped at 16,000 rows."""
    with pytest.raises(SqlProofScaleError, match="measured up to 16,000") as exc_info:
        _result().spills_below(16_001)
    assert "max_factor" in str(exc_info.value)


def test_spills_below_beyond_the_measured_range_with_a_spill_below_is_true():
    """An observed spill at or below `rows` answers the question whatever
    range was measured: the sweep did see it spill by then."""
    pts = [_pt(1, 100), _pt(2, 200), _pt(4, 400, temp=500), _pt(8, 800, temp=900)]
    assert _result(points=pts).spills_below(1_000_000) is True


def test_spills_below_uses_total_rows_across_the_profile():
    pts = [_pt(1, 100), _pt(2, 200), _pt(4, 400, temp=500), _pt(8, 800, temp=900)]
    r = _result(points=pts)
    assert r.spill_point_rows == 4000
    assert r.factor_at_spill == 4
    assert r.spills_below(5000) is True
    assert r.spills_below(3000) is False


def test_spills_below_boundary_is_inclusive():
    """The spill lands at exactly total_rows=4000. `spills_below` is
    documented as "at or below" -- checking only 5000 and 3000, both
    far from the edge, lets a `<` vs `<=` mutant survive. Assert at the
    boundary itself, and one row to either side."""
    pts = [_pt(1, 100), _pt(2, 200), _pt(4, 400, temp=500), _pt(8, 800, temp=900)]
    r = _result(points=pts)
    assert r.spills_below(4000) is True
    assert r.spills_below(3999) is False
    assert r.spills_below(4001) is True


def test_rows_before_timeout_returns_a_band():
    """The default `_result()` fixture has no spill, a healthy exponent
    (1.0) and an untruncated sweep, so `rows_before_timeout` must
    produce a real band rather than None.

    Computed the way `fit.py:project_rows_before_timeout` does: last
    point is factor=16, total_rows=16_000, exec_ms=1.0. ratio =
    10_000 / 1.0 = 10_000. centre = 16_000 * 10_000 ** (1/1.0) =
    160_000_000. lo = int(centre * 0.6) = 96_000_000 (already above
    the last measured total_rows, so the floor clamp is a no-op).
    hi = int(centre * 1.6) = 256_000_000.
    """
    band = _result().rows_before_timeout(10_000)
    assert isinstance(band, tuple)
    assert band == (96_000_000, 256_000_000)


def test_rows_before_timeout_uses_the_last_regime_not_the_first():
    """Same points as `test_rows_before_timeout_returns_a_band`, but the
    exponent now comes from the second regime of a multi-regime
    fixture. A `regimes[0]` mutant would fit exponent=2.0 instead of
    1.0 and project a wildly different centre (1_600_000 rather than
    160_000_000), so the band pinned below only holds if the *last*
    regime was used.
    """
    r = _result(regimes=[
        FitResult(2.0, 0.99, None, 1, 4, "a"),
        FitResult(1.0, 0.999, None, 4, 16, "a"),
    ])
    assert r.rows_before_timeout(10_000) == (96_000_000, 256_000_000)


def test_truncated_sweep_refuses_to_project():
    assert _result(truncated=True).rows_before_timeout(10_000) is None


def test_rows_before_timeout_with_no_regimes_returns_none():
    """No stable regime means no exponent to project from. Must return
    None like every other refusal in this method, not raise -- raising
    on an inconclusive fit is `.exponent`'s job, not this one's. The
    guard is unexercised by every other test here since they all
    supply at least one regime."""
    flips = [PlanFlip(2, "a", "b")]
    r = _result(regimes=[], plan_flips=flips)
    assert r.rows_before_timeout(10_000) is None
