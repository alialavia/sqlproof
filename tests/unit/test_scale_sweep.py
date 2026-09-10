"""The sweep controller's own decisions. No database: every name the
controller imports from `load`, `args` and `probe` is monkeypatched
*as imported into `sqlproof.scale.sweep`*, so the live integration
suite is the only place these interactions actually touch Postgres.
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from sqlproof.exceptions import SqlProofScaleError
from sqlproof.scale import sweep
from sqlproof.scale.probe import ProbePoint
from sqlproof.scale.sweep import run_sweep
from sqlproof.schema.model import SchemaInfo


def _install_db_stubs(monkeypatch: pytest.MonkeyPatch, probe_fn: Any) -> None:
    monkeypatch.setattr(sweep, "truncate", lambda conn, schema: None)
    monkeypatch.setattr(
        sweep, "load_dataset", lambda conn, schema, sizes, *, seed, columns: None
    )
    monkeypatch.setattr(sweep, "analyze", lambda conn, schema: None)
    monkeypatch.setattr(sweep, "resolve_args", lambda conn, args: ())
    monkeypatch.setattr(sweep, "probe_function", probe_fn)


def _point(factor: int, total_rows: int, work: int, plan_hash: str = "h") -> ProbePoint:
    return ProbePoint(
        factor=factor, total_rows=total_rows, work_blocks=work,
        peak_memory_kb=0, temp_blocks=0, plan_hash=plan_hash, exec_ms=1.0,
    )


def test_baseline_comes_from_the_calibration_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calibration work is 100, ladder work is 100 + f**2 -- an exponent
    of exactly 2.0 only comes out if the calibration probe's work_blocks
    is what gets subtracted. Baselining on the factor-1 point (work=101,
    which would zero out that very point and refuse the fit) or on 0
    (which leaves the constant term in and understates the exponent)
    both give a detectably different result."""

    def fake_probe(conn, function, args, *, factor, total_rows):
        work = 100 if factor == 0 else 100 + factor**2
        return _point(factor, total_rows, work)

    _install_db_stubs(monkeypatch, fake_probe)
    result = run_sweep(
        object(), SchemaInfo(), "fn", sizes={"t": 10}, max_factor=64,
    )
    assert abs(result.exponent - 2.0) < 1e-6


def test_ladder_stops_once_five_points_fit_a_clean_power_law(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With max_factor=64 and a clean power law throughout, the ladder
    stops the moment 5 points fit (factors 1, 2, 4, 8, 16). Nothing is
    probed at 32."""
    probed_factors: list[int] = []

    def fake_probe(conn, function, args, *, factor, total_rows):
        probed_factors.append(factor)
        work = 100 if factor == 0 else 100 + factor**2
        return _point(factor, total_rows, work)

    _install_db_stubs(monkeypatch, fake_probe)
    result = run_sweep(
        object(), SchemaInfo(), "fn", sizes={"t": 10}, max_factor=64,
    )
    assert [p.factor for p in result.points] == [1, 2, 4, 8, 16]
    assert 32 not in probed_factors


def test_timeout_marks_truncated_and_stops_probing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe that takes longer than probe_timeout_s sets
    truncated=True and no further factor is probed."""
    probed_factors: list[int] = []

    def fake_probe(conn, function, args, *, factor, total_rows):
        probed_factors.append(factor)
        if factor == 2:
            time.sleep(0.3)
        return _point(factor, total_rows, 100)

    _install_db_stubs(monkeypatch, fake_probe)
    result = run_sweep(
        object(), SchemaInfo(), "fn", sizes={"t": 10},
        max_factor=64, probe_timeout_s=0.05,
    )
    assert result.truncated is True
    assert probed_factors == [0, 1, 2]  # 0 is the calibration probe; 4 is never reached


def test_final_segment_decides_when_the_ladder_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Segment 'a' (factor 1 only) is frozen at a single point forever
    and can never itself satisfy the fit-quality check; segment 'b'
    (factor >= 2) is a clean power law. If the sweep checked the FIRST
    segment instead of the current (final) one, it would never see a
    passing fit and would run all the way to max_factor (64). Checking
    the final segment, it stops at 32, the moment 'b' accumulates its
    own 5 points."""

    def fake_probe(conn, function, args, *, factor, total_rows):
        if factor == 0:
            work = 100
        elif factor == 1:
            work = 999  # segment 'a': permanently 1 point, value irrelevant
        else:
            work = 100 + factor**2  # segment 'b'
        plan_hash = "a" if factor == 1 else "b"
        return _point(factor, total_rows, work, plan_hash)

    _install_db_stubs(monkeypatch, fake_probe)
    result = run_sweep(
        object(), SchemaInfo(), "fn", sizes={"t": 10}, max_factor=64,
    )
    assert [p.factor for p in result.points] == [1, 2, 4, 8, 16, 32]


def test_exponent_uses_the_final_regime_not_an_earlier_accepted_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """min_points=10 keeps the live stop-check from ever firing (only 7
    factors are reachable by max_factor=64), so segment 'a' -- a clean,
    fittable 5-point quadratic -- is frozen without ever being
    live-tested, while segment 'b' ends with too few points to fit.
    `.exponent` must raise using b's refusal, not silently fall back to
    a's accepted 2.0."""

    def fake_probe(conn, function, args, *, factor, total_rows):
        if factor == 0:
            work = 100
        elif factor <= 16:
            work = 100 + factor**2  # segment 'a': 1, 2, 4, 8, 16 -- clean quadratic
        else:
            work = 1  # segment 'b': 32, 64 -- only 2 points, never fittable
        plan_hash = "a" if factor <= 16 else "b"
        return _point(factor, total_rows, work, plan_hash)

    _install_db_stubs(monkeypatch, fake_probe)
    result = run_sweep(
        object(), SchemaInfo(), "fn", sizes={"t": 10},
        max_factor=64, min_points=10,
    )
    assert abs(result.regimes[0].exponent - 2.0) < 1e-6  # earlier regime IS accepted
    assert result.regimes[-1].exponent is None  # final regime is refused
    with pytest.raises(SqlProofScaleError):
        _ = result.exponent
