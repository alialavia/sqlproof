from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

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

    @st.composite
    def rows(draw: st.DrawFn) -> list[dict[str, Any]]:
        generated: list[dict[str, Any]] = []
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
            generated.append(row)
        return generated

    return rows()


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
        return draw(cast(SearchStrategy[Any], override))
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
