from __future__ import annotations

import pytest

from sqlproof.exceptions import CircularDependencyError
from sqlproof.schema.dependency_graph import insertion_order
from sqlproof.schema.fingerprint import compute
from sqlproof.schema.model import Column, ForeignKey, PgType, SchemaInfo, Table
from sqlproof.schema.parse_sql import parse_schema_sql


def test_schema_fingerprint_is_stable_for_equivalent_schema() -> None:
    integer = PgType(kind="scalar", name="integer")
    schema = SchemaInfo(
        tables=(
            Table(
                schema="public",
                name="customers",
                columns=(Column("id", integer, nullable=False, default=None, is_generated=False),),
                primary_key=("id",),
                foreign_keys=(),
                unique_constraints=(),
                check_constraints=(),
            ),
        ),
    )

    assert compute(schema) == compute(schema)
    assert compute(schema).startswith("sha256:")


def test_dependency_graph_orders_parent_tables_first() -> None:
    integer = PgType(kind="scalar", name="integer")
    customers = Table(
        schema="public",
        name="customers",
        columns=(Column("id", integer, False, None, False),),
        primary_key=("id",),
        foreign_keys=(),
        unique_constraints=(),
        check_constraints=(),
    )
    orders = Table(
        schema="public",
        name="orders",
        columns=(
            Column("id", integer, False, None, False),
            Column("customer_id", integer, False, None, False),
        ),
        primary_key=("id",),
        foreign_keys=(
            ForeignKey(("customer_id",), "customers", ("id",), "NO ACTION", "NO ACTION"),
        ),
        unique_constraints=(),
        check_constraints=(),
    )

    assert [table.name for table in insertion_order((orders, customers))] == ["customers", "orders"]


def test_dependency_graph_rejects_distinct_table_cycles() -> None:
    integer = PgType(kind="scalar", name="integer")
    left = Table(
        schema="public",
        name="left_table",
        columns=(
            Column("id", integer, False, None, False),
            Column("right_id", integer, False, None, False),
        ),
        primary_key=("id",),
        foreign_keys=(ForeignKey(("right_id",), "right_table", ("id",), "NO ACTION", "NO ACTION"),),
        unique_constraints=(),
        check_constraints=(),
    )
    right = Table(
        schema="public",
        name="right_table",
        columns=(
            Column("id", integer, False, None, False),
            Column("left_id", integer, False, None, False),
        ),
        primary_key=("id",),
        foreign_keys=(ForeignKey(("left_id",), "left_table", ("id",), "NO ACTION", "NO ACTION"),),
        unique_constraints=(),
        check_constraints=(),
    )

    with pytest.raises(CircularDependencyError):
        insertion_order((left, right))


def test_parse_schema_sql_models_tables_columns_keys_and_checks() -> None:
    schema = parse_schema_sql(
        """
        CREATE TYPE order_status AS ENUM ('pending', 'paid');

        CREATE TABLE customers (
          id SERIAL PRIMARY KEY,
          email VARCHAR(255) NOT NULL UNIQUE
        );

        CREATE TABLE orders (
          id SERIAL PRIMARY KEY,
          customer_id INTEGER NOT NULL REFERENCES customers(id),
          status order_status NOT NULL,
          total NUMERIC(10,2) NOT NULL CHECK (total >= 0)
        );
        """
    )

    assert [table.name for table in schema.tables] == ["customers", "orders"]
    assert schema.enums[0].name == "order_status"
    customers = schema.table("customers")
    orders = schema.table("orders")
    assert customers.primary_key == ("id",)
    assert customers.unique_constraints == (("email",),)
    assert orders.foreign_keys[0].referenced_table == "customers"
    assert orders.column("status").type.kind == "enum"
    assert orders.check_constraints[0].expression == "total >= 0"
