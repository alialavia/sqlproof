from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from sqlproof.generators.columns import strategy_for_column
from sqlproof.generators.constraints import refine_for_checks
from sqlproof.schema.model import ForeignKey, Table


def table_rows_strategy(
    table: Table,
    *,
    count: int,
    parent_rows: dict[str, list[dict[str, Any]]] | None = None,
) -> SearchStrategy[list[dict[str, Any]]]:
    parent_rows = parent_rows or {}

    @st.composite
    def rows(draw: st.DrawFn) -> list[dict[str, Any]]:
        generated: list[dict[str, Any]] = []
        for index in range(count):
            row: dict[str, Any] = {}
            for column in table.columns:
                if column.name in table.primary_key and len(table.primary_key) == 1:
                    row[column.name] = index + 1
                    continue
                if column.is_generated:
                    continue
                fk = _foreign_key_for_column(table, column.name)
                if fk is not None and fk.referenced_table in parent_rows:
                    parents = parent_rows[fk.referenced_table]
                    if parents:
                        parent = draw(st.sampled_from(parents))
                        row[column.name] = parent[fk.referenced_columns[0]]
                        continue
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


def _foreign_key_for_column(table: Table, column_name: str) -> ForeignKey | None:
    for foreign_key in table.foreign_keys:
        if foreign_key.columns == (column_name,):
            return foreign_key
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
