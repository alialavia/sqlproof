from __future__ import annotations

from typing import Any

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from sqlproof.schema.dependency_graph import insertion_order
from sqlproof.schema.model import SchemaInfo

from .rows import table_rows_strategy

Dataset = dict[str, list[dict[str, Any]]]


def dataset_strategy(schema: SchemaInfo, *, sizes: dict[str, int]) -> SearchStrategy[Dataset]:
    ordered_tables = insertion_order(schema.tables)

    @st.composite
    def dataset(draw: st.DrawFn) -> Dataset:
        rows_by_table: Dataset = {}
        for table in ordered_tables:
            count = sizes.get(table.name, 0)
            rows_by_table[table.name] = draw(
                table_rows_strategy(table, count=count, parent_rows=rows_by_table)
            )
        return rows_by_table

    return dataset()
