from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from sqlproof.schema.dependency_graph import insertion_order
from sqlproof.schema.model import SchemaInfo

from .rows import ColumnOverrides, table_rows_strategy

Dataset = dict[str, list[dict[str, Any]]]
SizeSpec = int | SearchStrategy[int]


def dataset_strategy(
    schema: SchemaInfo,
    *,
    sizes: Mapping[str, SizeSpec],
    external_parent_rows: Mapping[str, list[dict[str, Any]]] | None = None,
    columns: ColumnOverrides | None = None,
) -> SearchStrategy[Dataset]:
    ordered_tables = insertion_order(schema.tables)
    external_parent_rows = external_parent_rows or {}

    @st.composite
    def dataset(draw: st.DrawFn) -> Dataset:
        rows_by_table: Dataset = {}
        for table in ordered_tables:
            count = _draw_size(draw, sizes.get(table.name, 0))
            available_parent_rows = {**external_parent_rows, **rows_by_table}
            rows_by_table[table.name] = draw(
                table_rows_strategy(
                    table,
                    count=count,
                    parent_rows=available_parent_rows,
                    rows_by_table=rows_by_table,
                    columns=columns,
                )
            )
        return rows_by_table

    return dataset()


def _draw_size(draw: st.DrawFn, size: SizeSpec) -> int:
    if isinstance(size, int):
        return size
    return draw(size)
