from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from hypothesis import assume
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from sqlproof.exceptions import SqlProofGenerationError
from sqlproof.generators.columns import strategy_for_column
from sqlproof.generators.constraints import refine_for_checks
from sqlproof.schema.model import ForeignKey, Table

DatasetRows = dict[str, list[dict[str, Any]]]
ColumnOverrides = Mapping[str, Any]


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
                if column.name in table.primary_key and len(table.primary_key) == 1:
                    row[column.name] = _unique_value(column.name, column.type.name, index)
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
                    row[column.name] = _draw_override(draw, override, context)
                    continue
                if column.default is not None:
                    continue
                fk = _foreign_key_for_column(table, column.name)
                if fk is not None:
                    parent_key = _parent_rows_key(fk, parent_rows)
                    if parent_key is not None:
                        parents = parent_rows[parent_key]
                        if parents:
                            parent = draw(st.sampled_from(parents))
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
                    row[column.name] = _unique_value(column.name, column.type.name, index)
                    continue
                strategy = refine_for_checks(
                    column, strategy_for_column(column), table.check_constraints
                )
                row[column.name] = draw(strategy)

            # Composite uniqueness check. SQL's standard UNIQUE
            # semantics treat NULL as distinct (no two NULLs collide),
            # so skip the check for tuples containing a None — that
            # matches Postgres's default behavior on UNIQUE
            # constraints. (NULLs NOT DISTINCT is opt-in via NOT NULL
            # or `NULLS NOT DISTINCT`; we don't track that variant.)
            for key in composite_unique_keys:
                if any(col not in row or row[col] is None for col in key):
                    continue
                tuple_value = tuple(row[col] for col in key)
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
                tuple_value = tuple(row[col] for col in cols)
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


def _unique_value(column_name: str, type_name: str, index: int) -> Any:
    normalized = type_name.lower()
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
