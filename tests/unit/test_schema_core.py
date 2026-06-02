from __future__ import annotations

import pytest

from sqlproof.exceptions import CircularDependencyError
from sqlproof.schema.dependency_graph import insertion_order
from sqlproof.schema.fingerprint import compute
from sqlproof.schema.introspect import introspect_schema
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


def test_parse_schema_sql_uses_postgres_ast_for_qualified_constraints() -> None:
    schema = parse_schema_sql(
        """
        CREATE TYPE app.order_status AS ENUM ('pending', 'paid');

        CREATE TABLE app.customers (
          id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY
        );

        CREATE TABLE app.orders (
          id INTEGER PRIMARY KEY,
          customer_id INTEGER NOT NULL,
          status app.order_status NOT NULL DEFAULT 'pending',
          total NUMERIC(10, 2) NOT NULL,
          CONSTRAINT orders_total_non_negative CHECK (total >= 0),
          CONSTRAINT orders_customer_fk
            FOREIGN KEY (customer_id) REFERENCES app.customers(id) ON DELETE CASCADE,
          UNIQUE (customer_id, status)
        );
        """,
        schema="app",
    )

    customers = schema.table("customers", schema="app")
    orders = schema.table("orders", schema="app")

    assert schema.enums[0].name == "order_status"
    assert customers.column("id").identity == "always"
    assert orders.column("status").type.kind == "enum"
    assert orders.column("status").default == "'pending'"
    assert orders.foreign_keys[0].columns == ("customer_id",)
    assert orders.foreign_keys[0].referenced_table == "customers"
    assert orders.foreign_keys[0].referenced_schema == "app"
    assert orders.foreign_keys[0].on_delete == "CASCADE"
    assert orders.unique_constraints == (("customer_id", "status"),)
    assert orders.check_constraints[0].expression == "total >= 0"


class FakeIntrospectionConnection:
    def execute(self, sql: str, params: tuple[object, ...] = ()) -> object:
        del params
        if "pg_type" in sql and "enum_values" in sql:
            return FakeRows(
                [
                    {
                        "schema_name": "public",
                        "enum_name": "order_status",
                        "enum_values": ["pending", "paid"],
                    }
                ]
            )
        if "contype = 'p'" in sql:
            return FakeRows(
                [{"schema_name": "public", "table_name": "customers", "columns": ["id"]}]
            )
        if "contype = 'u'" in sql:
            return FakeRows(
                [{"schema_name": "public", "table_name": "customers", "columns": ["email"]}]
            )
        if "contype = 'f'" in sql:
            return FakeRows(
                [
                    {
                        "schema_name": "public",
                        "table_name": "orders",
                        "columns": ["customer_id"],
                        "referenced_schema": "auth",
                        "referenced_table": "customers",
                        "referenced_columns": ["id"],
                        "on_delete": "NO ACTION",
                        "on_update": "NO ACTION",
                    }
                ]
            )
        if "contype = 'c'" in sql:
            return FakeRows(
                [
                    {
                        "schema_name": "public",
                        "table_name": "orders",
                        "expression": "customer_id > 0",
                    }
                ]
            )
        if "pg_proc" in sql:
            return FakeRows([])
        if "idx.indpred" in sql:
            # Partial unique indexes — none in this fixture's schema.
            return FakeRows([])
        if "contype = 'x'" in sql:
            # Exclusion constraints — none in this fixture's schema.
            return FakeRows([])
        if "pg_attribute att" in sql and "column_name" in sql:
            return FakeRows(
                [
                    {
                        "schema_name": "public",
                        "table_name": "customers",
                        "column_name": "id",
                        "type_name": "integer",
                        "nullable": False,
                        "default": "nextval('customers_id_seq'::regclass)",
                        "is_generated": False,
                        "identity": None,
                        "modifiers": [],
                    },
                    {
                        "schema_name": "public",
                        "table_name": "customers",
                        "column_name": "email",
                        "type_name": "text",
                        "nullable": False,
                        "default": None,
                        "is_generated": False,
                        "identity": None,
                        "modifiers": [],
                    },
                    {
                        "schema_name": "public",
                        "table_name": "orders",
                        "column_name": "customer_id",
                        "type_name": "integer",
                        "nullable": False,
                        "default": None,
                        "is_generated": False,
                        "identity": None,
                        "modifiers": [],
                    },
                ]
            )
        raise AssertionError(f"Unexpected introspection SQL: {sql}")


class FakeRows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows


def test_introspect_schema_models_catalog_columns_constraints_and_enums() -> None:
    schema = introspect_schema(FakeIntrospectionConnection())

    assert schema.enums[0].enum_values == ("pending", "paid")
    customers = schema.table("customers")
    orders = schema.table("orders")
    assert customers.primary_key == ("id",)
    assert customers.unique_constraints == (("email",),)
    assert orders.foreign_keys[0].referenced_table == "customers"
    assert orders.foreign_keys[0].referenced_schema == "auth"
    assert orders.check_constraints[0].expression == "customer_id > 0"
