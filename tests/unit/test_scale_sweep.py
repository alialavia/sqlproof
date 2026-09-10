"""The sweep controller's own decisions. No database: every name the
controller imports from `load`, `args` and `probe` that touches the
database is monkeypatched *as imported into `sqlproof.scale.sweep`*, so
the live integration suite is the only place these interactions actually
touch Postgres. (`insertion_plan` and `missing_required_parents` are
pure, so they run for real here, against small modelled schemas.)
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest

from sqlproof.exceptions import SqlProofScaleError, SqlProofUsageError
from sqlproof.scale import sweep
from sqlproof.scale.fit import MIN_POINTS
from sqlproof.scale.probe import ProbePoint
from sqlproof.scale.sweep import run_sweep
from sqlproof.schema.model import Column, ForeignKey, PgType, SchemaInfo, Table
from sqlproof.schema.parse_sql import parse_schema_sql

_INTEGER = PgType(name="integer", kind="base")


def _schema(*names: str) -> SchemaInfo:
    """A model holding tables with these names and no foreign keys --
    all the sweep's up-front checks need to accept a `sizes` profile
    naming them."""
    return SchemaInfo(
        tables=tuple(
            Table(
                schema="public", name=name, columns=(), primary_key=(),
                foreign_keys=(), unique_constraints=(), check_constraints=(),
            )
            for name in names
        )
    )


def _install_db_stubs(
    monkeypatch: pytest.MonkeyPatch,
    probe_fn: Any,
    log: list[tuple[Any, ...]] | None = None,
    *,
    counts: dict[str, int] | None = None,
    truncate_fn: Callable[[], None] | None = None,
) -> None:
    """Stub every database-touching name `sweep` imports. When `log` is
    given, `row_counts`/`truncate`/`load_dataset`/`analyze`/
    `probe_function` each append an entry to it -- ("row_counts",),
    ("truncate",), ("load", dict(sizes)), ("analyze",), ("probe",
    factor, total_rows) -- so a test can pin the exact shape and order
    of what the controller does, not just the final result (Ruling R #1
    / Important 2). `counts` is what `row_counts` reports (every table
    empty unless given); `truncate_fn`, when given, runs inside every
    truncate, so a test can make one fail."""

    def _row_counts(conn: Any, schema: Any) -> dict[str, int]:
        if log is not None:
            log.append(("row_counts",))
        return {table.name: (counts or {}).get(table.name, 0) for table in schema.tables}

    def _truncate(conn: Any, schema: Any) -> None:
        if log is not None:
            log.append(("truncate",))
        if truncate_fn is not None:
            truncate_fn()

    def _load_dataset(conn: Any, schema: Any, sizes: Any, *, seed: Any, columns: Any) -> None:
        if log is not None:
            log.append(("load", dict(sizes)))

    def _analyze(conn: Any, schema: Any) -> None:
        if log is not None:
            log.append(("analyze",))

    def _probe(conn: Any, function: Any, args: Any, *, factor: int, total_rows: int) -> Any:
        if log is not None:
            log.append(("probe", factor, total_rows))
        return probe_fn(conn, function, args, factor=factor, total_rows=total_rows)

    monkeypatch.setattr(sweep, "row_counts", _row_counts)
    monkeypatch.setattr(sweep, "truncate", _truncate)
    monkeypatch.setattr(sweep, "load_dataset", _load_dataset)
    monkeypatch.setattr(sweep, "analyze", _analyze)
    monkeypatch.setattr(sweep, "resolve_args", lambda conn, args: ())
    monkeypatch.setattr(sweep, "probe_function", _probe)


def _point(factor: int, total_rows: int, work: int, plan_hash: str = "h") -> ProbePoint:
    return ProbePoint(
        factor=factor, total_rows=total_rows, work_blocks=work,
        peak_memory_kb=0, temp_blocks=0, plan_hash=plan_hash, exec_ms=1.0,
    )


def _quadratic_probe(conn, function, args, *, factor, total_rows):
    """Calibration work 100; ladder work 100 + f**2 -- exponent 2.0."""
    work = 100 if factor == 0 else 100 + factor**2
    return _point(factor, total_rows, work)


def _never_probe(conn, function, args, *, factor, total_rows):
    raise AssertionError("the sweep must refuse before it reaches a probe")


def test_baseline_comes_from_the_calibration_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calibration work is 100, ladder work is 100 + f**2 -- an exponent
    of exactly 2.0 only comes out if the calibration probe's work_blocks
    is what gets subtracted. Baselining on the factor-1 point (work=101,
    which would zero out that very point and refuse the fit) or on 0
    (which leaves the constant term in and understates the exponent)
    both give a detectably different result.

    Ruling R #1 / Important 2: the result alone doesn't pin the SHAPE
    of what got there -- a full-size calibration load, a bare warm
    probe with no truncate/load/analyze, or a missing ANALYZE anywhere
    could all still land on the same exponent by coincidence with this
    particular fake probe. Two differently-sized tables (so the
    one-row calibration map is distinguishable from the full-size
    ladder map) plus a call log pin the exact sequence: count the rows
    already there (Ruling AM), then truncate, load {a:1,b:1}, analyze,
    probe 0 -- twice (Ruling T) -- then truncate, load {a:10,b:30},
    analyze, probe 1 with total_rows scaled by factor, and so on for
    every ladder probe, and finally one more truncate (Ruling AM)."""
    log: list[tuple[Any, ...]] = []

    _install_db_stubs(monkeypatch, _quadratic_probe, log=log)
    result = run_sweep(
        object(), _schema("a", "b"), "fn", sizes={"a": 10, "b": 30}, max_factor=64,
    )
    assert abs(result.exponent - 2.0) < 1e-6

    assert log[0] == ("row_counts",)
    assert log[1:9] == [
        ("truncate",),
        ("load", {"a": 1, "b": 1}),
        ("analyze",),
        ("probe", 0, 0),
        ("truncate",),
        ("load", {"a": 1, "b": 1}),
        ("analyze",),
        ("probe", 0, 0),
    ]
    assert log[9:13] == [
        ("truncate",),
        ("load", {"a": 10, "b": 30}),
        ("analyze",),
        ("probe", 1, 40),
    ]

    # Global Constraint: every ladder probe's total_rows is the full
    # profile scaled by its own factor (sum(sizes.values()) * factor),
    # never a per-table count or an unscaled constant.
    ladder_probes = [entry for entry in log if entry[0] == "probe" and entry[1] >= 1]
    assert ladder_probes == [
        ("probe", 1, 40), ("probe", 2, 80), ("probe", 4, 160),
        ("probe", 8, 320), ("probe", 16, 640),
    ]
    # Ruling AM: the last probe is followed by one more truncate, so a
    # finished sweep leaves the modelled tables empty.
    assert log[-2:] == [("probe", 16, 640), ("truncate",)]


def test_calibration_keeps_the_second_rounds_work_not_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling T: the calibration round (truncate -> one-row load ->
    analyze -> resolve_args -> probe_function) runs TWICE and the
    FIRST round's work is discarded -- it pays plpgsql's one-time
    compile cost, which no ladder point ever pays. The first
    calibration probe here returns 500 (standing in for that compile
    tax); the second returns 100 (the steady replan cost every ladder
    point actually experiences). Ladder work is 100 + f**2, so an
    exponent of exactly 2.0 only comes out if the SECOND round's 100
    is kept as baseline: keeping the first (500) would refuse every
    ladder point outright (100 + f**2 - 500 <= 0 for small f)."""
    calibration_calls = 0

    def fake_probe(conn, function, args, *, factor, total_rows):
        nonlocal calibration_calls
        if factor == 0:
            calibration_calls += 1
            work = 500 if calibration_calls == 1 else 100
            return _point(factor, total_rows, work)
        return _point(factor, total_rows, 100 + factor**2)

    _install_db_stubs(monkeypatch, fake_probe)
    result = run_sweep(
        object(), _schema("t"), "fn", sizes={"t": 10}, max_factor=64,
    )
    assert calibration_calls == 2  # the round ran twice
    assert abs(result.exponent - 2.0) < 1e-6


def test_calibration_loads_at_most_one_row_per_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ruling AM: calibration loads `min(count, 1)` rows per table, so a
    table sized 0 stays empty at calibration exactly as it does on the
    ladder, instead of getting one row there and none anywhere else."""
    log: list[tuple[Any, ...]] = []
    _install_db_stubs(monkeypatch, _quadratic_probe, log=log)
    run_sweep(object(), _schema("a", "b"), "fn", sizes={"a": 10, "b": 0}, max_factor=64)
    loads = [entry for entry in log if entry[0] == "load"]
    assert loads[:3] == [
        ("load", {"a": 1, "b": 0}),
        ("load", {"a": 1, "b": 0}),
        ("load", {"a": 10, "b": 0}),
    ]


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
        object(), _schema("t"), "fn", sizes={"t": 10}, max_factor=64,
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
        object(), _schema("t"), "fn", sizes={"t": 10},
        max_factor=64, probe_timeout_s=0.05,
    )
    assert result.truncated is True
    # Two calibration probes (factor 0, run twice per Ruling T), then the
    # ladder; 4 is never reached.
    assert probed_factors == [0, 0, 1, 2]


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
        object(), _schema("t"), "fn", sizes={"t": 10}, max_factor=64,
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
        object(), _schema("t"), "fn", sizes={"t": 10},
        max_factor=64, min_points=10,
    )
    assert abs(result.regimes[0].exponent - 2.0) < 1e-6  # earlier regime IS accepted
    assert result.regimes[-1].exponent is None  # final regime is refused
    with pytest.raises(SqlProofScaleError):
        _ = result.exponent


# --- Ruling AM: nothing destructive happens until every check passes ---


def test_an_unknown_sizes_key_is_refused_before_any_database_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misspelt key would load no rows for that table yet still count
    toward every point's total_rows."""
    log: list[tuple[Any, ...]] = []
    _install_db_stubs(monkeypatch, _never_probe, log=log)
    with pytest.raises(SqlProofUsageError, match="does not model: evnets") as exc_info:
        run_sweep(
            object(), _schema("orgs", "events"), "fn",
            sizes={"orgs": 1, "evnets": 10},
        )
    assert "Modelled tables: events, orgs" in str(exc_info.value)
    assert log == []


def test_min_points_below_the_fits_minimum_is_refused_before_any_database_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fit never accepts fewer than MIN_POINTS points, so a smaller
    min_points would silently do nothing. (Every other test here runs at
    the default, min_points == MIN_POINTS, so the boundary itself is
    accepted.)"""
    log: list[tuple[Any, ...]] = []
    _install_db_stubs(monkeypatch, _never_probe, log=log)
    with pytest.raises(SqlProofUsageError, match=r"below fit\.MIN_POINTS"):
        run_sweep(
            object(), _schema("t"), "fn", sizes={"t": 10}, min_points=MIN_POINTS - 1,
        )
    assert log == []


def test_an_unsafe_function_name_is_refused_before_any_database_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling AN, at the sweep: the probe is stubbed out here, so this
    pins run_sweep's own check -- nothing is counted or truncated."""
    log: list[tuple[Any, ...]] = []
    _install_db_stubs(monkeypatch, _never_probe, log=log)
    with pytest.raises(SqlProofUsageError, match="invalid identifier segment"):
        run_sweep(
            object(), _schema("t"),
            "rv.noop(); COMMIT; DELETE FROM rv.canary; SELECT rv.noop",
            sizes={"t": 10},
        )
    assert log == []


FK_SCHEMA_SQL = """
CREATE TABLE orgs (id bigint PRIMARY KEY, name text NOT NULL);
CREATE TABLE events (
  id bigint PRIMARY KEY,
  org_id bigint NOT NULL REFERENCES orgs(id)
);
CREATE TABLE notes (id bigint PRIMARY KEY, org_id bigint REFERENCES orgs(id));
"""


@pytest.mark.parametrize("sizes", [{"events": 10}, {"orgs": 0, "events": 10}])
def test_a_required_parent_left_empty_is_refused_before_any_database_call(
    monkeypatch: pytest.MonkeyPatch, sizes: dict[str, int],
) -> None:
    """Measured against the live loader: with no parent rows, a NOT NULL
    foreign key fails in the generator -- after the truncate, before
    this check existed."""
    log: list[tuple[Any, ...]] = []
    _install_db_stubs(monkeypatch, _never_probe, log=log)
    schema = parse_schema_sql(FK_SCHEMA_SQL, schema="public")
    with pytest.raises(SqlProofUsageError, match=r"events\.org_id -> orgs"):
        run_sweep(object(), schema, "fn", sizes=sizes)
    assert log == []


def test_a_nullable_parent_left_empty_is_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loader writes NULL into a nullable foreign key with no parent
    rows, so this profile loads fine and must not be refused."""
    _install_db_stubs(monkeypatch, _quadratic_probe)
    schema = parse_schema_sql(FK_SCHEMA_SQL, schema="public")
    result = run_sweep(object(), schema, "fn", sizes={"notes": 10}, max_factor=64)
    assert [p.factor for p in result.points] == [1, 2, 4, 8, 16]


def _cyclic_schema(*, nullable: bool) -> SchemaInfo:
    """a.b_id -> b and b.a_id -> a. With a nullable a.b_id the planner can
    defer that edge, and the bulk loader then refuses the deferred edge;
    with every foreign key NOT NULL there is nothing to defer."""

    def table(name: str, fk_column: str, parent: str, fk_nullable: bool) -> Table:
        return Table(
            schema="public",
            name=name,
            columns=(
                Column("id", _INTEGER, nullable=False, default=None, is_generated=False),
                Column(fk_column, _INTEGER, nullable=fk_nullable, default=None,
                       is_generated=False),
            ),
            primary_key=("id",),
            foreign_keys=(
                ForeignKey((fk_column,), parent, ("id",), "NO ACTION", "NO ACTION"),
            ),
            unique_constraints=(),
            check_constraints=(),
        )

    return SchemaInfo(tables=(table("a", "b_id", "b", nullable), table("b", "a_id", "a", False)))


@pytest.mark.parametrize(
    ("nullable", "loader_says"),
    [
        (True, "does not yet support foreign-key cycles"),
        (False, "Circular foreign-key dependency"),
    ],
)
def test_a_schema_the_loader_cannot_load_is_refused_before_any_database_call(
    monkeypatch: pytest.MonkeyPatch, nullable: bool, loader_says: str,
) -> None:
    """The check is the loader's own planning step (`insertion_plan`,
    which `load_dataset` also calls), not a reimplementation of it."""
    log: list[tuple[Any, ...]] = []
    _install_db_stubs(monkeypatch, _never_probe, log=log)
    with pytest.raises(SqlProofUsageError, match=loader_says) as exc_info:
        run_sweep(object(), _cyclic_schema(nullable=nullable), "fn", sizes={"a": 2, "b": 2})
    assert exc_info.value.__cause__ is not None  # chained from the loader's error
    assert log == []


def test_tables_holding_rows_are_refused_and_nothing_is_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[tuple[Any, ...]] = []
    _install_db_stubs(monkeypatch, _never_probe, log=log, counts={"a": 3})
    with pytest.raises(SqlProofUsageError) as exc_info:
        run_sweep(object(), _schema("a", "b"), "fn", sizes={"a": 10, "b": 30})
    message = str(exc_info.value)
    assert "public.a (3 rows)" in message
    assert "public.b" not in message  # only the non-empty table is named
    assert "EMPTIES AND REPOPULATES" in message
    assert "truncate_existing=True" in message
    assert log == [("row_counts",)]


def test_truncate_existing_skips_the_row_count_and_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    log: list[tuple[Any, ...]] = []
    _install_db_stubs(monkeypatch, _quadratic_probe, log=log, counts={"a": 3})
    result = run_sweep(
        object(), _schema("a"), "fn", sizes={"a": 10}, max_factor=64,
        truncate_existing=True,
    )
    assert ("row_counts",) not in log
    assert log[0] == ("truncate",)
    assert abs(result.exponent - 2.0) < 1e-6


def _probe_failing_at_factor_two(conn, function, args, *, factor, total_rows):
    if factor == 2:
        raise RuntimeError("function raised mid-sweep")
    return _quadratic_probe(conn, function, args, factor=factor, total_rows=total_rows)


def test_a_failing_sweep_still_empties_the_tables_and_its_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[tuple[Any, ...]] = []
    _install_db_stubs(monkeypatch, _probe_failing_at_factor_two, log=log)
    with pytest.raises(RuntimeError, match="function raised mid-sweep") as exc_info:
        run_sweep(object(), _schema("t"), "fn", sizes={"t": 10})
    assert log[-2:] == [("probe", 2, 20), ("truncate",)]
    assert not getattr(exc_info.value, "__notes__", [])


def test_a_failing_final_truncate_does_not_mask_the_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The truncate that follows a failure fails too (a dropped
    connection, say). The caller must still see the ORIGINAL error --
    with a note that the tables may still hold synthetic rows -- not the
    cleanup's."""
    log: list[tuple[Any, ...]] = []

    def truncate_after_the_failure() -> None:
        if ("probe", 2, 20) in log:
            raise OSError("connection lost")

    _install_db_stubs(
        monkeypatch, _probe_failing_at_factor_two, log=log,
        truncate_fn=truncate_after_the_failure,
    )
    with pytest.raises(RuntimeError, match="function raised mid-sweep") as exc_info:
        run_sweep(object(), _schema("t"), "fn", sizes={"t": 10})
    notes = "\n".join(getattr(exc_info.value, "__notes__", []))
    assert "failed to empty the modelled tables" in notes
    assert "OSError: connection lost" in notes
    assert log[-1] == ("truncate",)  # the cleanup was attempted
