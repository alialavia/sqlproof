from __future__ import annotations

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from sqlproof.generators.graph import Dataset, dataset_strategy
from sqlproof.schema.model import Column, PgType, SchemaInfo, Table


def schemas(max_tables: int = 3, max_columns: int = 5) -> SearchStrategy[SchemaInfo]:
    del max_columns
    table_names = st.lists(
        st.sampled_from(["users", "orders", "products", "scores", "events"]),
        min_size=1,
        max_size=max_tables,
        unique=True,
    )

    def build(names: list[str]) -> SchemaInfo:
        integer = PgType("scalar", "integer")
        tables = tuple(
            Table(
                schema="public",
                name=name,
                columns=(Column("id", integer, False, None, False),),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(),
            )
            for name in names
        )
        return SchemaInfo(tables=tables)

    return table_names.map(build)


def datasets_for(schema: SchemaInfo, sizes: dict[str, int]) -> SearchStrategy[Dataset]:
    return dataset_strategy(schema, sizes=sizes)
