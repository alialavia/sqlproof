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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from sqlproof import _database as database
from sqlproof.exceptions import (
    CircularDependencyError,
    SqlProofGenerationError,
    SqlProofScaleError,
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
from sqlproof.scale.probe import ProbePoint, ProbeTimedOut, probe_function
from sqlproof.scale.result import ScaleResult
from sqlproof.schema.model import SchemaInfo

if TYPE_CHECKING:
    from sqlproof.scenarios.model import Scenario


@dataclass(frozen=True, slots=True)
class LoadStep:
    """What to load at one scale factor (0 is the calibration round)."""

    sizes: Mapping[str, int]
    columns: ColumnOverrides | None = None
    refs: Mapping[str, Any] = field(default_factory=dict[str, Any])
    """Named-row keys, for `ref(...)` arguments."""
    before_load: Callable[[database.Connection], None] | None = None


Profile = Callable[[int], LoadStep]

DISTRIBUTIONS = ("uniform", "zipf")
"""How the sweep spreads children over parents (`bulk.parent_index_for`)."""


def sizes_profile(sizes: Mapping[str, int], columns: ColumnOverrides | None) -> Profile:
    """`sizes` times the factor; at most one row per table to calibrate."""

    def step(factor: int) -> LoadStep:
        if factor == 0:
            return LoadStep({name: min(count, 1) for name, count in sizes.items()}, columns)
        return LoadStep({name: count * factor for name, count in sizes.items()}, columns)

    return step


class ScenarioProfile:
    """A scenario at every factor: `Scenario.scaled(factor)`, and
    `Scenario.minimal()` to calibrate. Seeds the scenario's `auth.users`
    rows before each load, and `cleanup` removes them at the end."""

    def __init__(self, scenario: Scenario, schema: SchemaInfo, *, scale_cohorts: bool) -> None:
        self.scenario = scenario
        self.schema = schema
        self.scale_cohorts = scale_cohorts
        self._auth_ids: set[str] = set()

    def __call__(self, factor: int) -> LoadStep:
        from sqlproof.scenarios.load import (
            _insert_auth_users,  # pyright: ignore[reportPrivateUsage]
        )

        variant = (
            self.scenario.minimal()
            if factor == 0
            else self.scenario.scaled(factor, scale_cohorts=self.scale_cohorts)
        )
        compiled = variant.compile(self.schema)
        users = compiled.auth_users

        def before_load(conn: database.Connection) -> None:
            _insert_auth_users(conn, users)
            self._auth_ids.update(user.id for user in users)

        return LoadStep(compiled.sizes, compiled.columns, compiled.refs, before_load)

    def cleanup(self, conn: database.Connection) -> None:
        if self._auth_ids:
            conn.execute(
                "DELETE FROM auth.users WHERE id = ANY(%s::uuid[])", [sorted(self._auth_ids)]
            ).close()


def run_sweep(
    conn: database.Connection,
    schema: SchemaInfo,
    function: str,
    *,
    sizes: Mapping[str, int] | None = None,
    scenario: Scenario | None = None,
    scale_cohorts: bool = False,
    args: Sequence[Any] = (),
    max_factor: int = 32,
    min_points: int = 5,
    probe_timeout_s: float = 30.0,
    seed: int | None = None,
    columns: ColumnOverrides | None = None,
    truncate_existing: bool = False,
    distribution: str = "uniform",
    nested_plans: bool = False,
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
    `args.py`). `seed` fixes the generated data, and with it every
    point's row counts, resolved arguments and plans -- and so the
    fitted exponent. Each probe counts only the buffers of the tables
    `schema` models, with their indexes and TOAST storage (see
    `probe_function`'s `relations`), not the system catalogs: catalog
    reads varied with per-backend cache state and with other sessions'
    catalog changes, and moved a single probe by tens of blocks. The
    calibration baseline is taken on the same connection as the ladder.
    `columns` pins generated columns, as `load_dataset`'s does.
    `distribution` spreads children over their parents: `"uniform"`
    (default) or `"zipf"`, where a few parents own most children -- the
    skew real tenants have, and the one `heaviest` then finds.
    `nested_plans` makes the plans of the statements inside the function
    count, through auto_explain (see `probe_function`): a plan change in
    a PL/pgSQL function then splits the fit, and its sorts report memory.

    Instead of `sizes`, pass a `scenario` (`sqlproof.scenarios`): its
    compiled sizes are the 1x profile. At factor `k` its fills and
    per-parent counts grow `k` times, while cohorts (unless
    `scale_cohorts`) and named rows stay fixed -- "does it still work for
    these two customers as the rest grows". Arguments may be references
    (`ref("acme")`), resolved to the named row's key at each factor. The
    scenario's seed is the sweep's; its `auth.users` rows are inserted
    before each load and removed at the end. `columns` cannot be combined
    with a scenario: pin columns in the scenario.

    The ladder stops at the first of: `min_points` points whose final
    plan regime fits (exponent standard error at most
    `fit.MAX_SLOPE_STDERR`); `max_factor` reached;
    or a probe taking longer than `probe_timeout_s`, which marks the
    result truncated. Postgres cancels such a probe at the timeout
    (`statement_timeout`); the cancelled point is not kept, since it
    measured nothing. A timeout during calibration -- on at most one row
    per table -- raises `SqlProofScaleError` instead: there is nothing to
    measure a curve from. A probe that returns just under the timeout but
    took longer overall (planning, transfer) is kept and still ends the
    ladder.
    """
    profile: Profile
    scenario_profile: ScenarioProfile | None = None
    scenario_info: dict[str, object] | None = None
    if (sizes is None) == (scenario is None):
        msg = "Pass exactly one of sizes or scenario."
        raise SqlProofUsageError(msg)
    if distribution not in DISTRIBUTIONS:
        msg = f"distribution must be one of {', '.join(DISTRIBUTIONS)}; got {distribution!r}."
        raise SqlProofUsageError(msg)
    if scenario is not None:
        if columns is not None:
            msg = "columns cannot be combined with a scenario: pin the columns in the scenario."
            raise SqlProofUsageError(msg)
        if seed is not None and seed != scenario.seed:
            msg = (
                f"seed={seed} conflicts with scenario {scenario.name!r}'s seed "
                f"{scenario.seed}; the scenario's seed is the sweep's."
            )
            raise SqlProofUsageError(msg)
        seed = scenario.seed
        try:
            scenario_profile = ScenarioProfile(scenario, schema, scale_cohorts=scale_cohorts)
            first = scenario_profile(1)
        except Exception as exc:
            msg = f"The sweep refused before truncating anything: {exc}"
            raise SqlProofUsageError(msg) from exc
        profile = scenario_profile
        base_sizes: Mapping[str, int] = first.sizes
        scenario_info = {
            "name": scenario.name,
            "fingerprint": scenario.fingerprint,
            "scale_cohorts": scale_cohorts,
        }
    else:
        assert sizes is not None
        seed = 0 if seed is None else seed
        profile = sizes_profile(sizes, columns)
        base_sizes = sizes
    _validate(schema, function, sizes=base_sizes, min_points=min_points)
    if not truncate_existing:
        _refuse_non_empty_tables(conn, schema)

    def clean_up() -> None:
        truncate(conn, schema)
        if scenario_profile is not None:
            scenario_profile.cleanup(conn)

    try:
        result = _measure(
            conn, schema, function,
            profile=profile, args=args, max_factor=max_factor,
            min_points=min_points, probe_timeout_s=probe_timeout_s, seed=seed,
            distribution=distribution, nested_plans=nested_plans,
        )
    except BaseException as original:
        try:
            clean_up()
        except Exception as cleanup_error:
            original.add_note(
                "The sweep also failed to empty the modelled tables after this "
                f"error ({type(cleanup_error).__name__}: {cleanup_error}); they "
                "may still hold synthetic rows."
            )
        raise
    clean_up()
    if scenario_info is not None:
        result = replace(result, scenario=scenario_info)
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
    modelled = {table.name for table in schema.tables} | {
        table.qualified_name for table in schema.tables
    }
    unknown = sorted(name for name in sizes if name not in modelled)
    if unknown:
        msg = (
            f"sizes names tables the schema does not model: {', '.join(unknown)}. "
            f"Modelled tables: {', '.join(sorted(t.name for t in schema.tables)) or '(none)'}. A "
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


def _with_refs(args: Sequence[Any], refs: Mapping[str, Any]) -> list[Any]:
    """Replace each `ref("name")` argument with that named row's key."""
    from sqlproof.scenarios.model import RowRef

    out: list[Any] = []
    for arg in args:
        if isinstance(arg, RowRef):
            if arg.name not in refs:
                msg = (
                    f"Argument {arg!r} names no row of the scenario; "
                    f"named rows: {', '.join(sorted(refs)) or 'none'}."
                )
                raise SqlProofUsageError(msg)
            out.append(refs[arg.name])
        else:
            out.append(arg)
    return out


def _refuse_non_empty_tables(conn: database.Connection, schema: SchemaInfo) -> None:
    counts = row_counts(conn, schema)
    occupied: list[str] = []
    for table in schema.tables:
        count = counts.get(table.name, 0)
        if count is None:
            occupied.append(
                f"{table.qualified_name} (row-level security hides its rows "
                "from this role, so it cannot be verified empty)"
            )
        elif count > 0:
            occupied.append(f"{table.qualified_name} ({count} rows)")
    if occupied:
        msg = (
            "The sweep refused to start: it EMPTIES AND REPOPULATES every "
            f"modelled table, and these are not known to be empty: {', '.join(occupied)}. "
            "Nothing was truncated. Point the sweep at a dedicated test "
            "database, or pass truncate_existing=True to let it delete these "
            "rows."
        )
        raise SqlProofUsageError(msg)


def _measure(
    conn: database.Connection,
    schema: SchemaInfo,
    function: str,
    *,
    profile: Profile,
    args: Sequence[Any],
    max_factor: int,
    min_points: int,
    probe_timeout_s: float,
    seed: int,
    distribution: str = "uniform",
    nested_plans: bool = False,
) -> ScaleResult:
    """Calibrate, climb the ladder and fit -- the part of the sweep that
    writes to the database, run only after `run_sweep`'s checks pass."""
    points: list[ProbePoint] = []
    truncated = False

    # Calibrate the fixed per-call cost -- the modelled tables' pages a
    # call touches regardless of data size -- on a separate one-row
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
    # One row per table at most (`profile(0)`), so a table sized 0 stays
    # empty here exactly as it does at every ladder point.

    # Count only the modelled tables' buffers: see probe_function.
    relations = [(table.schema, table.name) for table in schema.tables]

    def _load(step: LoadStep) -> None:
        truncate(conn, schema)
        if step.before_load is not None:
            step.before_load(conn)
        load_dataset(
            conn, schema, step.sizes, seed=seed, columns=step.columns,
            distribution=distribution,
        )
        analyze(conn, schema)

    def _calibration_round() -> ProbePoint:
        step = profile(0)
        _load(step)
        calibration_args = resolve_args(conn, _with_refs(args, step.refs))
        try:
            return probe_function(
                conn, function, calibration_args, factor=0, total_rows=0,
                relations=relations, timeout_s=probe_timeout_s,
                nested_plans=nested_plans,
            )
        except ProbeTimedOut as exc:
            msg = (
                f"{exc}, on the calibration dataset of at most one row per table. "
                "There is no curve to measure; raise probe_timeout_s or check that "
                "the function returns."
            )
            raise SqlProofScaleError(msg) from exc

    # Since probes count only the modelled tables, the compile and replan
    # work described above (catalog reads) no longer reaches work_blocks.
    # The first round is still discarded: the first scan of freshly
    # loaded rows touches one more block than later scans.
    _calibration_round()  # discarded
    baseline = _calibration_round().work_blocks

    factor = 1
    while factor <= max_factor:
        step = profile(factor)
        _load(step)

        resolved = resolve_args(conn, _with_refs(args, step.refs))
        started = time.perf_counter()
        try:
            point = probe_function(
                conn, function, resolved,
                factor=factor, total_rows=sum(step.sizes.values()),
                relations=relations, timeout_s=probe_timeout_s,
                nested_plans=nested_plans,
            )
        except ProbeTimedOut:
            truncated = True
            break
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
        sizes=dict(profile(1).sizes),
        baseline=baseline,
        seed=seed,
        max_factor=max_factor,
        min_points=min_points,
        probe_timeout_s=probe_timeout_s,
        argument_policy=argument_policy(args),
        distribution=distribution,
        nested_plans=nested_plans,
    )
