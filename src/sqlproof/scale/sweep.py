"""Drive load -> probe across scale factors, then fit.

The ladder stops on FIT QUALITY, not a time budget. That follows from
leading with the exponent rather than a breaking point: measuring a
complexity class needs enough points to fit a curve, not enough rows to
reach production scale. The Phase 1 spike recovered a clean quadratic
from 2K-32K rows in under a second of query time per point.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg

from sqlproof.generators.rows import ColumnOverrides
from sqlproof.scale.args import resolve_args
from sqlproof.scale.fit import fit_exponent, segment_by_plan
from sqlproof.scale.load import analyze, load_dataset, truncate
from sqlproof.scale.probe import ProbePoint, probe_function
from sqlproof.scale.result import ScaleResult
from sqlproof.schema.model import SchemaInfo


def run_sweep(
    conn: psycopg.Connection,
    schema: SchemaInfo,
    function: str,
    *,
    sizes: Mapping[str, int],
    args: Sequence[Any] = (),
    max_factor: int = 32,
    min_points: int = 5,
    probe_timeout_s: float = 30.0,
    seed: int = 0,
    columns: ColumnOverrides | None = None,
) -> ScaleResult:
    base_total = sum(sizes.values())
    points: list[ProbePoint] = []
    truncated = False

    # Calibrate the fixed per-call cost -- catalog lookups and plan
    # caching, paid regardless of data size -- on a separate one-row
    # load, never on a ladder point. Fitting a ladder point against its
    # own work_blocks subtracts to exactly 0 at that point and refuses
    # every sweep. Leaving the cost in the fit flattens the curve and
    # understates the exponent (the Phase 1 spike measured 1.69 raw
    # against 1.994 corrected on an exactly-quadratic function).
    #
    # The round runs TWICE and only the second is kept. A plpgsql
    # function's internal query plan is compiled once per connection,
    # the first time it is called -- that one-time compile is not paid
    # by any ladder point, since calibration always runs first. Every
    # ladder point instead follows truncate -> load -> analyze, which
    # invalidates the cached plan and forces a REPLAN, cheaper than a
    # fresh compile but still real work (measured on a reference
    # function: 43 blocks to compile once vs. 31 to replan after
    # invalidation, vs. 1-3 fully warm with no invalidation at all). A
    # single calibration round pays the compile cost no ladder point
    # pays and overstates the baseline; running it twice and discarding
    # the first absorbs that one-time tax, leaving the second round's
    # work_blocks measuring the same replan cost every ladder point
    # measures. A bare warm-up call directly followed by the measured
    # call is not equivalent: with no truncate/analyze between them
    # there is no invalidation, so the measured call would be fully
    # warm and understate the baseline. Neither calibration probe is
    # appended to `points`; the factor/total_rows sentinels below are
    # never part of any fit.
    def _calibration_round() -> ProbePoint:
        truncate(conn, schema)
        load_dataset(conn, schema, {name: 1 for name in sizes}, seed=seed, columns=columns)
        analyze(conn, schema)
        calibration_args = resolve_args(conn, args)
        return probe_function(
            conn, function, calibration_args, factor=0, total_rows=0,
        )

    _calibration_round()  # discarded: absorbs the one-time compile cost
    baseline = _calibration_round().work_blocks

    factor = 1
    while factor <= max_factor:
        scaled = {name: count * factor for name, count in sizes.items()}
        truncate(conn, schema)
        load_dataset(conn, schema, scaled, seed=seed, columns=columns)
        analyze(conn, schema)

        resolved = resolve_args(conn, args)
        started = time.perf_counter()
        point = probe_function(
            conn, function, resolved,
            factor=factor, total_rows=base_total * factor,
        )
        elapsed = time.perf_counter() - started
        points.append(point)

        if elapsed > probe_timeout_s:
            truncated = True
            break

        if len(points) >= min_points:
            segments, _flips = segment_by_plan(points)
            trial = fit_exponent(segments[-1], baseline)
            if trial.exponent is not None:
                break

        factor *= 2

    segments, flips = segment_by_plan(points)
    regimes = [fit_exponent(segment, baseline) for segment in segments]
    return ScaleResult(
        points=points,
        regimes=regimes,
        plan_flips=flips,
        truncated=truncated,
        function=function,
        sizes=dict(sizes),
    )
