"""Fitting a complexity exponent to measured work. Pure: no SQL, no I/O.

This module carries the bulk of the feature's test coverage, and it is
the seam a cloud tier reuses verbatim -- swap "measured locally" for
"ingested from a remote run" and the maths is unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from sqlproof.scale.probe import ProbePoint

MIN_POINTS = 5
MIN_R_SQUARED = 0.98


@dataclass(frozen=True, slots=True)
class FitResult:
    exponent: float | None
    r_squared: float | None
    reason: str | None
    from_factor: int
    to_factor: int
    plan_hash: str


def fit_exponent(points: Sequence[ProbePoint], baseline: int) -> FitResult:
    """Least-squares slope of log(work - baseline) against log(factor).

    The slope IS the complexity exponent: work proportional to n^k plots
    as a straight line of slope k on log-log axes.

    `baseline` is the fixed per-call cost -- catalog lookups and plan
    caching, ~100-190 blocks regardless of data size. Leaving it in
    flattens the curve toward zero at small factors and understates the
    exponent; the Phase 1 spike measured 1.69 raw against 1.994
    corrected on an exactly-quadratic function.
    """
    lo = points[0].factor if points else 0
    hi = points[-1].factor if points else 0
    shape = points[0].plan_hash if points else ""

    if len(points) < MIN_POINTS:
        return FitResult(
            None, None,
            f"only {len(points)} points, need at least {MIN_POINTS} to fit",
            lo, hi, shape,
        )

    xs: list[float] = []
    ys: list[float] = []
    for point in points:
        adjusted = point.work_blocks - baseline
        if adjusted <= 0 or point.factor <= 0:
            return FitResult(
                None, None,
                "work at or below the fixed baseline; nothing left to fit "
                f"(work={point.work_blocks}, baseline={baseline})",
                lo, hi, shape,
            )
        xs.append(math.log(point.factor))
        ys.append(math.log(adjusted))

    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return FitResult(None, None, "all points at the same factor", lo, hi, shape)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx

    intercept = mean_y - slope * mean_x
    ss_res = sum(
        (y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=True)
    )
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r2 = 1.0 if ss_tot == 0 else 1 - ss_res / ss_tot

    if r2 < MIN_R_SQUARED:
        return FitResult(
            None, round(r2, 4),
            f"r_squared {r2:.3f} below {MIN_R_SQUARED}; the points do not "
            "lie on a power curve, so no exponent is claimed",
            lo, hi, shape,
        )
    return FitResult(round(slope, 4), round(r2, 4), None, lo, hi, shape)
