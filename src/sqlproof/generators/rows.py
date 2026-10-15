from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import time, timedelta
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from typing import Any, cast
from uuid import UUID

from hypothesis import assume
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from sqlproof.exceptions import SqlProofGenerationError
from sqlproof.generators.columns import strategy_for_column
from sqlproof.generators.constraints import refine_for_checks, warn_unhonored_checks
from sqlproof.generators.narrowing import (
    array_element_type,
    element_domain_checks,
    narrow_array_elements,
)
from sqlproof.generators.typespec import (
    DATE_WINDOW,
    DATETIME_WINDOW,
    TypeSpec,
    spec_for_type,
    spec_is_empty,
)
from sqlproof.schema.model import CheckConstraint, Column, ForeignKey, PgType, Table

DatasetRows = dict[str, list[dict[str, Any]]]
ColumnOverrides = Mapping[str, Any]


class _Generate(Enum):
    GENERATE = "GENERATE"

    def __repr__(self) -> str:
        return "GENERATE"


GENERATE = _Generate.GENERATE
"""Return this from a callable `columns=` override to mean "generate this
value as if the column had no override". Both generators honour it, so a
caller can pin a column for some rows and leave the rest to the
generator -- which is how scenario cohorts compile."""


@dataclass(frozen=True, slots=True)
class ColumnContext:
    table: Table
    column_name: str
    row_index: int
    row: dict[str, Any]
    table_rows: list[dict[str, Any]]
    rows_by_table: DatasetRows


def table_rows_strategy(
    table: Table,
    *,
    count: int,
    parent_rows: DatasetRows | None = None,
    rows_by_table: DatasetRows | None = None,
    columns: ColumnOverrides | None = None,
) -> SearchStrategy[list[dict[str, Any]]]:
    parent_rows = parent_rows or {}
    rows_by_table = rows_by_table or {}
    columns = columns or {}

    # Composite UNIQUEs (>1 column) and composite PRIMARY KEYs both
    # require the generator to produce distinct tuples across rows.
    # Single-column uniqueness is handled inline via _unique_value;
    # composite uniqueness is checked AFTER the row is built. Use
    # tuples so Hypothesis's shrinking can keep the closure deterministic.
    composite_unique_keys: tuple[tuple[str, ...], ...] = _composite_unique_keys(table)

    # Partial unique indexes (`CREATE UNIQUE INDEX ... WHERE ...`)
    # apply uniqueness only to rows where the predicate holds. We
    # compile each predicate to a callable that returns True / False
    # / None — None means "the simple evaluator can't read this
    # predicate" and the generator falls back to skipping enforcement
    # (Postgres still rejects at INSERT if the generated batch
    # happens to collide).
    partial_unique_checks: tuple[tuple[tuple[str, ...], _PredicateFn], ...] = tuple(
        (pu.columns, _compile_predicate(pu.predicate))
        for pu in table.partial_unique_constraints
    )

    # Loop-invariant: the refined strategy depends only on the column
    # and the table's checks, never on the row index. Skip exactly the
    # columns the per-row loop below skips before it reaches
    # refine_for_checks: single-column primary key, generated columns,
    # columns with a `columns=` override, columns with a default,
    # foreign-key columns, and single-column-unique columns (those are
    # all generated via other, cheaper paths inline in the loop). The
    # override skip matters beyond cost: strategy_for_column can RAISE
    # for a schema-valid-but-underspecified type (e.g. an undimensioned
    # `vector` column), and an override is exactly the caller's escape
    # hatch for such a column — the per-row loop never reaches
    # strategy_for_column for it, so the hoist must not either.
    # Building this per row cost ~2x at n=40,000 (see the scale
    # analysis design doc).
    refined_by_column: dict[str, SearchStrategy[Any]] = {}
    unhonored_scope: list[CheckConstraint] = list(table.check_constraints)
    for column in table.columns:
        if column.name in table.primary_key and len(table.primary_key) == 1:
            continue
        if column.is_generated:
            continue
        if _column_override(columns, table, column.name) is not None:
            continue
        if column.default is not None:
            continue
        if _foreign_key_for_column(table, column.name) is not None:
            continue
        if _is_single_column_unique(table, column.name):
            continue
        domain_checks = _domain_checks_as_column_checks(column)
        unhonored_scope.extend(domain_checks)
        unhonored_scope.extend(_element_checks_as_column_checks(column))
        refined_by_column[column.name] = refine_for_checks(
            column,
            strategy_for_column(column),
            table.check_constraints + domain_checks,
        )
    # Columns drawn from `refined_by_column` are the only ones whose
    # values come from a type strategy; a CHECK sqlproof can't read
    # that touches one of them may be violated, so say so.
    warn_unhonored_checks(table, unhonored_scope, refined_by_column)

    def refined_strategy(column: Column) -> SearchStrategy[Any]:
        """The column's CHECK-refined strategy, built on first use for a
        column the hoist above skipped because it carries an override:
        only an override that returns `GENERATE` ever needs it, and an
        always-pinned column must never reach `strategy_for_column`."""
        strategy = refined_by_column.get(column.name)
        if strategy is None:
            domain_checks = _domain_checks_as_column_checks(column)
            strategy = refine_for_checks(
                column,
                strategy_for_column(column),
                table.check_constraints + domain_checks,
            )
            refined_by_column[column.name] = strategy
            warn_unhonored_checks(
                table,
                [
                    *table.check_constraints,
                    *domain_checks,
                    *_element_checks_as_column_checks(column),
                ],
                [column.name],
            )
        return strategy

    # Loop-invariant: one sampled_from per FK column, not per row.
    # Constructing it per row is O(len(parents)) each time.
    parent_strategy_by_column: dict[str, SearchStrategy[dict[str, Any]]] = {}
    for column in table.columns:
        fk = _foreign_key_for_column(table, column.name)
        if fk is None:
            continue
        parent_key = _parent_rows_key(fk, parent_rows)
        if parent_key is None:
            continue
        available = parent_rows[parent_key]
        if available:
            parent_strategy_by_column[column.name] = st.sampled_from(available)

    @st.composite
    def rows(draw: st.DrawFn) -> list[dict[str, Any]]:
        generated: list[dict[str, Any]] = []
        # Track seen composite-key tuples per constraint. We only
        # compare rows where ALL columns of the constraint are
        # non-NULL (SQL's standard UNIQUE semantics: NULL is distinct).
        seen_per_key: dict[tuple[str, ...], set[tuple[Any, ...]]] = {
            key: set() for key in composite_unique_keys
        }
        # Track seen tuples per partial unique. Keyed by the column
        # tuple (predicate text isn't part of the key — two partial
        # uniques over the same column set would collide on this dict
        # but that's a degenerate schema we don't need to support).
        seen_per_partial: dict[tuple[str, ...], set[tuple[Any, ...]]] = {
            cols: set() for cols, _pred in partial_unique_checks
        }
        for index in range(count):
            row: dict[str, Any] = {}
            for column in table.columns:
                single_key = column.name in table.primary_key and len(table.primary_key) == 1
                if single_key and not isinstance(
                    _column_override(columns, table, column.name), PinnedKey
                ):
                    row[column.name] = _unique_value(column.name, column.type, index)
                    continue
                if column.is_generated:
                    continue
                override = _column_override(columns, table, column.name)
                if override is not None:
                    context = ColumnContext(
                        table=table,
                        column_name=column.name,
                        row_index=index,
                        row=row,
                        table_rows=generated,
                        rows_by_table=rows_by_table,
                    )
                    value = _draw_override(draw, override, context)
                    if value is not GENERATE:
                        row[column.name] = value
                        continue
                    if single_key:
                        row[column.name] = _unique_value(column.name, column.type, index)
                        continue
                if column.default is not None:
                    continue
                fk = _foreign_key_for_column(table, column.name)
                if fk is not None:
                    parent_strategy = parent_strategy_by_column.get(column.name)
                    if parent_strategy is not None:
                        parent = draw(parent_strategy)
                        row[column.name] = parent[fk.referenced_columns[0]]
                        continue
                    if column.nullable:
                        row[column.name] = None
                        continue
                    msg = (
                        f"Cannot generate {table.name}.{column.name}: "
                        f"required foreign key has no available parent rows for "
                        f"{fk.referenced_table}.{fk.referenced_columns[0]}."
                    )
                    raise SqlProofGenerationError(msg)
                if _is_single_column_unique(table, column.name):
                    row[column.name] = _unique_value(column.name, column.type, index)
                    continue
                row[column.name] = draw(refined_strategy(column))

            # Composite uniqueness check. SQL's standard UNIQUE
            # semantics treat NULL as distinct (no two NULLs collide),
            # so skip the check for tuples containing a None — that
            # matches Postgres's default behavior on UNIQUE
            # constraints. (NULLs NOT DISTINCT is opt-in via NOT NULL
            # or `NULLS NOT DISTINCT`; we don't track that variant.)
            for key in composite_unique_keys:
                if any(col not in row or row[col] is None for col in key):
                    continue
                tuple_value = tuple(_hashable(row[col]) for col in key)
                # If we've seen this tuple before, ask Hypothesis to
                # invalidate this example and try different draws.
                # Discarded examples are silently retried; the test
                # framework only fails if Hypothesis can't find any
                # valid draw within `max_examples`/health-check
                # budget — which happens iff the user asked for more
                # rows than the constraint space allows (e.g.
                # `sizes={"org_members": 100}` with only 2 orgs x
                # 2 users in the FK pool).
                assume(tuple_value not in seen_per_key[key])
                seen_per_key[key].add(tuple_value)

            # Partial-unique-index check. Three states for each row /
            # predicate:
            #   - predicate is None      → evaluator couldn't read it;
            #                              skip enforcement
            #   - predicate(row) is False → row doesn't compete; skip
            #   - predicate(row) is True  → row competes; assume() if
            #                               its column-tuple matches
            #                               another competing row
            for cols, predicate_fn in partial_unique_checks:
                matches = predicate_fn(row)
                if matches is not True:
                    continue
                if any(col not in row or row[col] is None for col in cols):
                    continue
                tuple_value = tuple(_hashable(row[col]) for col in cols)
                assume(tuple_value not in seen_per_partial[cols])
                seen_per_partial[cols].add(tuple_value)

            generated.append(row)
        return generated

    return rows()


def _composite_unique_keys(table: Table) -> tuple[tuple[str, ...], ...]:
    """Composite UNIQUE / PRIMARY KEY constraints with more than one
    column, plus all-equality EXCLUSION constraints.

    Used by the row generator to enforce that no two rows produce the
    same tuple on any composite-unique column set. Single-column
    UNIQUEs are handled by `_unique_value` in the inline generation
    path (faster, deterministic per-index values); composite cases
    fall back to assume-based rejection sampling.

    The composite PRIMARY KEY is included because, semantically, a
    composite PK is a composite UNIQUE + NOT NULL combo. Postgres
    treats it the same way for INSERT rejection purposes.

    EXCLUSION constraints where every operator is ``=`` degrade to
    composite UNIQUE — they reject any pair of rows where every
    column is equal, which is the same rule as composite UNIQUE.
    Exclusion constraints with non-equality operators (e.g. ``&&``
    for range overlap) need range semantics to enforce and aren't
    surfaced here; Postgres will reject conflicting INSERTs at
    runtime.
    """
    keys: list[tuple[str, ...]] = []
    if len(table.primary_key) > 1:
        keys.append(table.primary_key)
    for unique in table.unique_constraints:
        if len(unique) > 1:
            keys.append(unique)
    for exclusion in table.exclusion_constraints:
        if all(op == "=" for _col, op in exclusion.columns_with_operators):
            keys.append(tuple(col for col, _op in exclusion.columns_with_operators))
    return tuple(keys)


class PinnedKey:
    """A single-column primary key's value per row, from a scenario that pins
    a key which is also a foreign key (`profiles.id -> auth.users`).

    Both generators honour an override on such a key only in this wrapper:
    a plain `columns=` override on a primary key stays ignored, because
    child tables find their parents' keys by arithmetic on generated keys.
    The scenario compiler makes sure nothing does that for a pinned key."""

    __slots__ = ("resolve",)

    def __init__(self, resolve: Callable[[Any], Any]) -> None:
        self.resolve = resolve

    def __call__(self, context: Any) -> Any:
        return self.resolve(context)


def _column_override(
    overrides: ColumnOverrides,
    table: Table,
    column_name: str,
) -> Any | None:
    for key in (f"{table.qualified_name}.{column_name}", f"{table.name}.{column_name}"):
        if key in overrides:
            return overrides[key]
    return None


def _draw_override(draw: st.DrawFn, override: Any, context: ColumnContext) -> Any:
    if isinstance(override, SearchStrategy):
        # `isinstance` narrows to bare `SearchStrategy` (parameter unknown) in
        # pyright; mypy infers `SearchStrategy[Any]` and rejects an explicit
        # cast as redundant. Suppressing the pyright-side complaint is the
        # least-bad option since `override` enters as `Any` and the runtime
        # call works with whatever element type the user supplied.
        return draw(override)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    if callable(override):
        return override(context)
    return override


def _foreign_key_for_column(table: Table, column_name: str) -> ForeignKey | None:
    for foreign_key in table.foreign_keys:
        if foreign_key.columns == (column_name,):
            return foreign_key
    return None


def _parent_rows_key(
    foreign_key: ForeignKey,
    parent_rows: dict[str, list[dict[str, Any]]],
) -> str | None:
    if foreign_key.referenced_schema is not None:
        qualified = f"{foreign_key.referenced_schema}.{foreign_key.referenced_table}"
        if qualified in parent_rows:
            return qualified
    if foreign_key.referenced_table in parent_rows:
        return foreign_key.referenced_table
    return None


def _is_single_column_unique(table: Table, column_name: str) -> bool:
    return any(columns == (column_name,) for columns in table.unique_constraints)


# Returns True/False if the predicate evaluates on `row`, or None if
# the simple evaluator can't read it (caller falls back to skipping
# uniqueness enforcement for that index).
_PredicateFn = Callable[[dict[str, Any]], bool | None]


# Predicate text may arrive parenthesized (`pg_get_expr` returns
# `(deleted_at IS NULL)` even when the source SQL was unparenthesized).
# Make the parens optional so the same compiler works on both
# parse_sql and introspect_schema outputs.
_IS_NULL_RE = re.compile(r"^\s*\(?\s*(\w+)\s+IS\s+NULL\s*\)?\s*$", re.IGNORECASE)
_IS_NOT_NULL_RE = re.compile(
    r"^\s*\(?\s*(\w+)\s+IS\s+NOT\s+NULL\s*\)?\s*$", re.IGNORECASE
)


def _compile_predicate(predicate: str) -> _PredicateFn:
    """Compile a partial-unique predicate into a Python callable.

    Supports the simple ``column IS NULL`` / ``column IS NOT NULL``
    shape (the canonical soft-delete pattern). Anything else returns
    None for every row, which the caller reads as "I can't evaluate
    this — skip enforcement." We intentionally don't try to be
    clever: misreading a predicate is worse than ignoring it
    (Postgres still rejects bad INSERTs).
    """
    null_match = _IS_NULL_RE.match(predicate)
    if null_match:
        column = null_match.group(1)
        return lambda row: row.get(column) is None
    not_null_match = _IS_NOT_NULL_RE.match(predicate)
    if not_null_match:
        column = not_null_match.group(1)
        return lambda row: row.get(column) is not None
    return lambda _row: None


_VALUE_PLACEHOLDER_RE = re.compile(r"\bvalue\b", re.IGNORECASE)


def _domain_checks_as_column_checks(column: Column) -> tuple[CheckConstraint, ...]:
    """Translate a domain's CHECK expressions to column-scoped
    CheckConstraints.

    Domain CHECKs reference ``VALUE`` (Postgres's placeholder for the
    domain value); the existing CHECK refiner works on expressions
    that name the column directly. We substitute the column name for
    ``VALUE`` so a single domain definition can be applied to many
    columns. Parser renderings can vary in case, so we match
    case-insensitively (and word-boundary, to avoid mangling tokens
    that happen to contain "value" as a substring).
    """
    pg_type = column.type
    if pg_type.kind != "domain" or not pg_type.check_expressions:
        return ()
    return tuple(
        CheckConstraint(expression=_VALUE_PLACEHOLDER_RE.sub(column.name, expr))
        for expr in pg_type.check_expressions
    )


def _element_checks_as_column_checks(column: Column) -> tuple[CheckConstraint, ...]:
    """An array column's element-domain CHECKs, VALUE replaced by the column
    name. narrowing.py honours the ones it can read on each element; these
    are only for the warning about the ones it cannot."""
    return tuple(
        CheckConstraint(expression=_VALUE_PLACEHOLDER_RE.sub(column.name, check.expression))
        for check in element_domain_checks(column.type)
    )


def _hashable(value: Any) -> Any:
    """`value` as a key for the seen-sets of composite and partial unique
    constraints: an array column's list becomes a tuple, a JSON document a
    tagged, order-insensitive form (recursively)."""
    if isinstance(value, list | tuple):
        return tuple(_hashable(item) for item in cast(Sequence[Any], value))
    if isinstance(value, dict):
        mapping = cast(dict[Any, Any], value)  # type: ignore[redundant-cast]
        return frozenset((key, _hashable(item)) for key, item in mapping.items())
    from sqlproof._database import Json  # lazily, as in columns.py

    if isinstance(value, Json):
        return (Json, _hashable(value.obj))
    return value


def _unique_value(column_name: str, pg_type: str | PgType, index: int) -> Any:
    """The `index`-th value of a single-column key or UNIQUE column.

    Takes the column's type, or just its name. With the type, an array
    column (or a domain over one) gets a value its element type accepts."""
    if isinstance(pg_type, PgType):
        element = array_element_type(pg_type)
        if element is not None:
            return _unique_array(column_name, pg_type, element, index)
        pg_type = pg_type.name
    normalized = pg_type.lower()
    if normalized.endswith("[]"):
        # An array column: a one-element array holding the element's value.
        return [_unique_value(column_name, normalized[:-2], index)]
    value = index + 1
    if normalized in {"smallint", "int2", "integer", "int", "int4", "serial"}:
        return value
    if normalized in {"bigint", "int8", "bigserial"}:
        return value
    if normalized in {"numeric", "decimal"}:
        return Decimal(value)
    if normalized == "uuid":
        return str(UUID(int=value))
    return f"{column_name}_{value}"


_DATE_SPAN = 1_000_000
"""Distinct days a unique date array cycles through before it repeats the
day to make the array longer (2000-01-01 plus a million days stays in
range for Postgres and Python)."""


@lru_cache(maxsize=256)
def _narrowed_element_spec(pg_type: PgType) -> TypeSpec | None:
    """An array type's element spec with its domain's CHECKs folded in;
    cached, since both generators ask once per row."""
    return narrow_array_elements(spec_for_type(pg_type), pg_type).element


def _unique_array(column_name: str, pg_type: PgType, element: PgType, index: int) -> list[Any]:
    """The `index`-th distinct array of `element` values, for a UNIQUE array
    column: element domains resolve to their base type, and an integer
    element keeps inside its domain's bounds (`pos[]`)."""
    spec = _narrowed_element_spec(pg_type)
    while element.kind == "domain" and element.base is not None:
        element = element.base
    if element.kind == "enum" and element.enum_values:
        return _counting_array(len(element.enum_values), element.enum_values.__getitem__, index)
    if spec is not None and spec.kind == "integer" and not spec_is_empty(spec):
        assert spec.min_value is not None and spec.max_value is not None
        low, high = int(spec.min_value), int(spec.max_value)
        return _counting_array(high - low + 1, _integer_from_one(low, high), index)
    name = element.name.lower()
    if name in {"bool", "boolean"}:
        return _counting_array(2, (False, True).__getitem__, index)
    if name in {"json", "jsonb"}:
        from sqlproof._database import Json  # lazily, as in columns.py

        return [Json(f"{column_name}_{index + 1}")]
    if name == "date":
        day = DATE_WINDOW[0] + timedelta(days=index % _DATE_SPAN)
        return [day] * (index // _DATE_SPAN + 1)
    if name.startswith("timestamp"):
        return [DATETIME_WINDOW[0] + timedelta(seconds=index)]
    if name in {"time", "timetz", "time without time zone", "time with time zone"}:
        seconds = index % 86_400
        moment = time(seconds // 3600, seconds // 60 % 60, seconds % 60)
        return [moment] * (index // 86_400 + 1)
    if name == "interval":
        return [timedelta(seconds=index + 1)]
    if name in {"real", "float4", "double precision", "float8"}:
        return [float(index + 1)]
    if name == "bytea":
        return [str(index + 1).encode()]
    return [_unique_value(column_name, name, index)]


def _counting_array(count: int, nth: Callable[[int], Any], index: int) -> list[Any]:
    """The `index`-th array over a closed set of `count` values (`nth(p)`
    is the p-th: an enum's labels, booleans, a domain's integers), counting
    in bijective base `count`: [a], [b], [a, a], [a, b], ... -- never
    empty, never repeated, and only logarithmically long."""
    digits: list[Any] = []
    remaining = index + 1
    while remaining > 0:
        remaining -= 1
        digits.append(nth(remaining % count))
        remaining //= count
    return digits



def _integer_from_one(low: int, high: int) -> Callable[[int], int]:
    """The `position`-th of the integers `low..high` ordered 1, 2, ... up
    to `high`, then 0, -1, ... down to `low` (starting at the nearer bound
    when 1 is outside them), so an unbounded integer array counts [1], [2],
    [3], ... as before."""
    start = min(max(1, low), high)
    upward = high - start + 1
    return lambda position: (
        start + position if position < upward else start - 1 - (position - upward)
    )
