"""Fitting a complexity exponent to measured work. Pure: no SQL, no I/O.

This module carries the bulk of the feature's test coverage, and it is
the seam a cloud tier reuses verbatim -- swap "measured locally" for
"ingested from a remote run" and the maths is unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from sqlproof.scale.probe import ProbePoint

MIN_POINTS = 5
MAX_SLOPE_STDERR = 0.15
"""A fit is accepted when the exponent's standard error is at most this.

At the minimum five points that puts the 95% interval within about
±0.5 of the reported exponent, narrower with more points. Real growth
measures far tighter (about 0.01 for the linear and quadratic reference
functions); only small, flat work comes near the limit. R² is not the test: for a flat series it
measures only the noise around a zero slope, so it refused well-behaved
O(1) functions (follow-up 2)."""


@dataclass(frozen=True, slots=True)
class FitResult:
    exponent: float | None
    r_squared: float | None
    reason: str | None
    from_factor: int
    to_factor: int
    plan_hash: str
    slope_stderr: float | None = None
    """Standard error of the exponent; what decides whether it is claimed."""


def fit_exponent(points: Sequence[ProbePoint], baseline: int) -> FitResult:
    """Least-squares slope of log(work - baseline) against log(factor).

    The slope IS the complexity exponent: work proportional to n^k plots
    as a straight line of slope k on log-log axes.

    `baseline` is the fixed per-call cost -- the modelled tables' pages a
    call touches regardless of data size. Leaving it in
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

    # Zero growth is a real answer, not a degenerate fit: a function
    # whose work is IDENTICAL at every factor, and does not exceed the
    # fixed baseline, is genuinely O(1) -- there is nothing to take the
    # log of, and the ordinary per-point loop below would otherwise
    # refuse it outright (adjusted <= 0 whenever the flat value is at
    # or below the baseline). A flat series ABOVE the baseline needs no
    # special case: it already fits exponent 0.0 (r^2 1.0) through the
    # ordinary path below, since a constant y against varying x has
    # zero slope.
    #
    # Ruling U: this must require every point EQUAL, not merely "no
    # point exceeds the first point's work" (the earlier guard) -- a
    # series that genuinely rises, falls, or spikes-then-flattens can
    # still satisfy "no point exceeds the first" (e.g. a spike at the
    # first point, or growth capped by an inflated baseline) without
    # being flat, and must stay refused below, not be reported as
    # constant.
    # Identical work at every factor above the baseline is exponent 0
    # too; handled here rather than trusting floating point to make the
    # residuals exactly zero.
    first_work = points[0].work_blocks
    if all(p.work_blocks == first_work for p in points):
        return FitResult(0.0, 1.0, None, lo, hi, shape, 0.0)

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
    # Reported, not tested: see MAX_SLOPE_STDERR.
    r2 = 1.0 if ss_tot <= 1e-12 else max(0.0, 1 - ss_res / ss_tot)
    stderr = math.sqrt(ss_res / (n - 2) / sxx)

    if stderr > MAX_SLOPE_STDERR:
        return FitResult(
            None, round(r2, 4),
            f"exponent {slope:.2f} has a standard error of {stderr:.3f}, above "
            f"{MAX_SLOPE_STDERR}; the points do not lie on a power curve, so "
            "no exponent is claimed",
            lo, hi, shape, round(stderr, 4),
        )
    return FitResult(round(slope, 4), round(r2, 4), None, lo, hi, shape, round(stderr, 4))


CPU_GUARD_MIN_MS = 20.0
"""Below this execution time at the last point, the time curve is mostly
fixed overhead and says nothing about growth."""
CPU_GUARD_MARGIN = 0.5
"""How far the time exponent may exceed the work exponent before the work
exponent is refused as blind to the growth (follow-up 3)."""
_CPU_GUARD_MAX_STDERR = 0.3


def cpu_growth_reason(points: Sequence[ProbePoint], work_exponent: float) -> str | None:
    """Why `work_exponent` misses growth the clock sees, or None.

    The exponent counts buffer blocks, so work that burns CPU without
    touching new pages -- a nested loop over a Materialize, an in-memory
    sort -- does not show in it. Wall-clock time does. When time grows
    clearly faster than work, the work exponent understates the cost and
    is refused rather than reported. Time never becomes the exponent: it
    depends on the machine, so it can only veto."""
    if len(points) < MIN_POINTS or points[-1].exec_ms < CPU_GUARD_MIN_MS:
        return None
    if any(p.exec_ms <= 0 or p.factor <= 0 for p in points):
        return None
    xs = [math.log(p.factor) for p in points]
    ys = [math.log(p.exec_ms) for p in points]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / sxx
    intercept = mean_y - slope * mean_x
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=True))
    if math.sqrt(ss_res / (n - 2) / sxx) > _CPU_GUARD_MAX_STDERR:
        return None  # the clock is too noisy to contradict the work
    if slope - work_exponent <= CPU_GUARD_MARGIN:
        return None
    return (
        f"execution time grows as about n^{slope:.2f} while buffer work grows as "
        f"n^{work_exponent:.2f}: CPU cost (in-memory joins, sorts or loops that "
        "touch no new pages) is growing faster than the I/O the exponent measures, "
        "so the exponent would understate it"
    )


@dataclass(frozen=True, slots=True)
class PlanFlip:
    at_factor: int
    from_hash: str
    to_hash: str


def segment_by_plan(
    points: Sequence[ProbePoint],
) -> tuple[list[list[ProbePoint]], list[PlanFlip]]:
    """Split points into runs sharing a plan shape, and report the
    boundaries.

    A plan change is a genuine discontinuity in the cost curve: at small
    n a sequential scan is correctly cheapest, and the planner switching
    to an index scan resets the curve rather than bending it. Fitting
    straight through one averages two different functions together.
    """
    if not points:
        return [], []
    segments: list[list[ProbePoint]] = [[points[0]]]
    flips: list[PlanFlip] = []
    for previous, current in pairwise(points):
        if current.plan_hash == previous.plan_hash:
            segments[-1].append(current)
            continue
        flips.append(
            PlanFlip(
                at_factor=current.factor,
                from_hash=previous.plan_hash,
                to_hash=current.plan_hash,
            )
        )
        segments.append([current])
    return segments, flips


def find_spill(points: Sequence[ProbePoint]) -> ProbePoint | None:
    """The first point where a sort or hash exceeded `work_mem`.

    Reported separately from the exponent because a spill is a cliff,
    not a curve. Everything is fine right up until it is not, and a
    least-squares fit run across that boundary reports a misleadingly
    gentle slope.
    """
    for point in points:
        if point.temp_blocks > 0:
            return point
    return None


def project_rows_before_timeout(
    points: Sequence[ProbePoint],
    fit: FitResult,
    timeout_ms: float,
    *,
    truncated: bool,
) -> tuple[int, int] | None:
    """Extrapolate the final regime to a wall-clock timeout.

    Returns a RANGE, never a point estimate, and returns None rather
    than guessing when the fit is unusable, the sweep was truncated, or
    the function already exceeds the timeout inside the measured range.

    This is the one output that depends on the machine that measured it.
    Every caller must label it as such; the exponent does not carry that
    caveat and the two must not be presented as equally solid.
    """
    if fit.exponent is None or truncated or not points:
        return None
    # A spill does not change plan_hash so it is fitted through,
    # producing R² near 1.0 and confident but wrong projections over a
    # cost cliff. Refuse rather than extrapolate through a discontinuity.
    if find_spill(points) is not None:
        return None
    last = points[-1]
    if last.exec_ms >= timeout_ms:
        return None
    if last.exec_ms <= 0 or fit.exponent <= 0:
        return None
    # time ~ rows^exponent, so rows_at_timeout = last_rows * ratio^(1/k)
    ratio = timeout_ms / last.exec_ms
    try:
        centre = last.total_rows * ratio ** (1.0 / fit.exponent)
    except OverflowError:
        # A near-zero exponent means cost does not grow: there is no row
        # count at which the function times out, so extrapolating one is
        # meaningless. Refuse rather than crash or claim absurd numbers.
        return None
    # Refuse projections exceeding 1e12 rows: beyond any plausible Postgres
    # table, a projection past it is an artifact of a near-flat exponent
    # rather than a measurement.
    if not math.isfinite(centre) or centre > 1e12:
        return None
    # A deliberately wide band. The exponent is measured, but wall-clock
    # is not stable enough (1.96x run to run on an idle machine) for a
    # tighter claim to be honest.
    lo = int(centre * 0.6)
    # Clamp the lower bound to the largest measured row count: we proved
    # that many rows complete under the timeout, so do not claim a lower
    # bound that contradicts our own data.
    lo = max(lo, last.total_rows)
    return lo, int(centre * 1.6)
