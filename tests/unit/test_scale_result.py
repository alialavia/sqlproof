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
    with pytest.raises(SqlProofScaleError, match="plan"):
        _ = r.exponent


def test_spills_below_is_false_when_nothing_spilled():
    assert _result().spills_below(1_000_000) is False
    assert _result().spill_point_rows is None


def test_spills_below_uses_total_rows_across_the_profile():
    pts = [_pt(1, 100), _pt(2, 200), _pt(4, 400, temp=500), _pt(8, 800, temp=900)]
    r = _result(points=pts)
    assert r.spill_point_rows == 4000
    assert r.factor_at_spill == 4
    assert r.spills_below(5000) is True
    assert r.spills_below(3000) is False


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


def test_truncated_sweep_refuses_to_project():
    assert _result(truncated=True).rows_before_timeout(10_000) is None
