"""Drive load -> probe across scale factors, then fit.

The ladder stops on FIT QUALITY, not a time budget. That follows from
leading with the exponent rather than a breaking point: measuring a
complexity class needs enough points to fit a curve, not enough rows to
reach production scale. The Phase 1 spike recovered a clean quadratic
from 2K-32K rows in under a second of query time per point.

The sweep is DESTRUCTIVE to the tables it models: it empties and
repopulates them at every scale factor. So `run_sweep` checks everything
it can before its first TRUNCATE, refuses to touch tables that already
hold rows unless told to, and empties them again when it finishes (Ruling
AM) -- see its docstring.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg

from sqlproof.exceptions import (
    CircularDependencyError,
    SqlProofGenerationError,
    SqlProofUsageError,
)
from sqlproof.generators.rows import ColumnOverrides
from sqlproof.scale._identifiers import validate_function_name
from sqlproof.scale.args import argument_policy, resolve_args
from sqlproof.scale.fit import MIN_POINTS, fit_exponent, segment_by_plan
from sqlproof.scale.load import (
    analyze,
    insertion_plan,
    load_dataset,
    missing_required_parents,
    row_counts,
    truncate,
)
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
    truncate_existing: bool = False,
) -> ScaleResult:
    """Measure how `function`'s buffer work grows as the `sizes` profile
    is scaled 1x, 2x, 4x, ..., and fit a complexity exponent to it.

    DESTRUCTIVE -- point it at a dedicated test database. The sweep
    EMPTIES AND REPOPULATES every table `schema` models: TRUNCATE, then
    COPY of generated rows, at every scale factor. On an autocommit
    connection (the one `scale_analysis` opens) each step commits as it
    goes, so nothing is rolled back. Before its first TRUNCATE it
    therefore:

    - checks what it can without the database: every `sizes` key names
      a modelled table; `min_points` is at least `fit.MIN_POINTS`;
      `function` is a bare, optionally schema-qualified name; and the
      loader can load the schema at these sizes -- no foreign-key cycle
      it cannot handle, no required foreign key whose parent `sizes`
      leaves empty. A failure raises `SqlProofUsageError` with nothing
      touched.
    - counts the rows in every modelled table, and refuses with
      `SqlProofUsageError`, naming each non-empty table, if any holds
      rows -- unless `truncate_existing=True`, the explicit opt-in to
      deleting them.

    When it finishes -- successfully or not -- it truncates the modelled
    tables once more, leaving them EMPTY rather than full of synthetic
    rows. If that final TRUNCATE fails while an earlier error is already
    propagating, the earlier error is re-raised, with a note, rather than
    masked.

    `sizes` is the 1x profile, rows per table; every row count the
    result reports is the total across it (`sum(sizes.values()) *
    factor`). `args` holds one entry per function parameter: a literal,
    or a resolver re-run against each freshly loaded dataset (see
    `args.py`). `seed` fixes the generated data, and so every point's
    row counts, plan shape and resolved arguments; buffer counts then
    repeat only to within a few blocks (up to 3 measured; most likely
    catalog lookups, which vary from run to run). `columns` pins
    generated columns, as `load_dataset`'s does.

    The ladder stops at the first of: `min_points` points whose final
    plan regime fits (R^2 >= `fit.MIN_R_SQUARED`); `max_factor` reached;
    or a probe taking longer than `probe_timeout_s`, which marks the
    result truncated. That timeout is checked AFTER a probe returns: a
    runaway probe is not cancelled, so a function that never returns
    hangs the sweep.
    """
    _validate(schema, function, sizes=sizes, min_points=min_points)
    if not truncate_existing:
        _refuse_non_empty_tables(conn, schema)
    try:
        result = _measure(
            conn, schema, function,
            sizes=sizes, args=args, max_factor=max_factor,
            min_points=min_points, probe_timeout_s=probe_timeout_s,
            seed=seed, columns=columns,
        )
    except BaseException as original:
        try:
            truncate(conn, schema)
        except Exception as cleanup_error:
            original.add_note(
                "The sweep also failed to empty the modelled tables after this "
                f"error ({type(cleanup_error).__name__}: {cleanup_error}); they "
                "may still hold synthetic rows."
            )
        raise
    truncate(conn, schema)
    return result


def _validate(
    schema: SchemaInfo,
    function: str,
    *,
    sizes: Mapping[str, int],
    min_points: int,
) -> None:
    """Everything checkable without the database, checked before the
    sweep touches it."""
    modelled = {table.name for table in schema.tables}
    unknown = sorted(name for name in sizes if name not in modelled)
    if unknown:
        msg = (
            f"sizes names tables the schema does not model: {', '.join(unknown)}. "
            f"Modelled tables: {', '.join(sorted(modelled)) or '(none)'}. A "
            "misspelt key would load no rows yet still count toward every "
            "point's total_rows."
        )
        raise SqlProofUsageError(msg)
    if min_points < MIN_POINTS:
        msg = (
            f"min_points={min_points} is below fit.MIN_POINTS ({MIN_POINTS}): "
            f"the fit never accepts fewer than {MIN_POINTS} points, so a "
            "smaller min_points would have no effect."
        )
        raise SqlProofUsageError(msg)
    validate_function_name(function)
    try:
        insertion_plan(schema)
    except (CircularDependencyError, SqlProofGenerationError) as exc:
        msg = (
            "The bulk loader cannot load this schema, so the sweep refused "
            f"before truncating anything: {exc}"
        )
        raise SqlProofUsageError(msg) from exc
    missing = missing_required_parents(schema, sizes)
    if missing:
        msg = (
            "sizes leaves the parent table of a required (NOT NULL) foreign "
            f"key empty, so these rows could never be loaded: {'; '.join(missing)}. "
            "Give each parent table a size, or leave the child table out of "
            "sizes. Nothing was truncated."
        )
        raise SqlProofUsageError(msg)


def _refuse_non_empty_tables(conn: psycopg.Connection, schema: SchemaInfo) -> None:
    counts = row_counts(conn, schema)
    occupied = [
        f"{table.qualified_name} ({counts[table.name]} rows)"
        for table in schema.tables
        if counts.get(table.name, 0) > 0
    ]
    if occupied:
        msg = (
            "The sweep refused to start: it EMPTIES AND REPOPULATES every "
            f"modelled table, and these already hold rows: {', '.join(occupied)}. "
            "Nothing was truncated. Point the sweep at a dedicated test "
            "database, or pass truncate_existing=True to let it delete these "
            "rows."
        )
        raise SqlProofUsageError(msg)


def _measure(
    conn: psycopg.Connection,
    schema: SchemaInfo,
    function: str,
    *,
    sizes: Mapping[str, int],
    args: Sequence[Any],
    max_factor: int,
    min_points: int,
    probe_timeout_s: float,
    seed: int,
    columns: ColumnOverrides | None,
) -> ScaleResult:
    """Calibrate, climb the ladder and fit -- the part of the sweep that
    writes to the database, run only after `run_sweep`'s checks pass."""
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
    #
    # One row per table at most: `min(count, 1)`, so a table sized 0
    # stays empty here exactly as it does at every ladder point.
    calibration_sizes = {name: min(count, 1) for name, count in sizes.items()}

    def _calibration_round() -> ProbePoint:
        truncate(conn, schema)
        load_dataset(conn, schema, calibration_sizes, seed=seed, columns=columns)
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
        baseline=baseline,
        seed=seed,
        max_factor=max_factor,
        min_points=min_points,
        probe_timeout_s=probe_timeout_s,
        argument_policy=argument_policy(args),
    )
