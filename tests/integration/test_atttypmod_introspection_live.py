"""Integration tests for atttypmod decoding via DSN introspection.

Verifies that varchar(n), numeric(p,s), and char(n) columns round-trip
their modifiers correctly through `introspect_schema`, and that the
end-to-end generator respects column constraints (no overflow or
length violations).

Covers issues #85 and #91.
"""

from __future__ import annotations

import os
from decimal import Decimal
from uuid import uuid4

import psycopg
import psycopg.rows
import pytest

SCHEMA_SQL = """
CREATE TABLE modifier_types (
    id serial PRIMARY KEY,
    name varchar(50) NOT NULL,
    code char(10) NOT NULL,
    price numeric(6, 2) NOT NULL
);
"""

pytestmark = pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)


@pytest.fixture()
def schema_and_dsn():
    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    schema_name = f"sqlproof_atttypmod_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            for stmt in SCHEMA_SQL.strip().split(";"):
                if stmt.strip():
                    conn.execute(
                        stmt.replace(
                            "CREATE TABLE modifier_types",
                            f'CREATE TABLE "{schema_name}".modifier_types',
                        )
                    )
            yield dsn, schema_name
        finally:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')


def test_varchar_modifiers_round_trip(schema_and_dsn) -> None:
    from sqlproof.schema.introspect import introspect_schema

    dsn, schema_name = schema_and_dsn
    with psycopg.connect(dsn) as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        schema = introspect_schema(cur, schema=schema_name)
    col = schema.table("modifier_types", schema=schema_name).column("name")
    assert col.type.name == "varchar"
    assert col.type.modifiers == (50,)


def test_char_modifiers_round_trip(schema_and_dsn) -> None:
    from sqlproof.schema.introspect import introspect_schema

    dsn, schema_name = schema_and_dsn
    with psycopg.connect(dsn) as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        schema = introspect_schema(cur, schema=schema_name)
    col = schema.table("modifier_types", schema=schema_name).column("code")
    assert col.type.name == "bpchar"
    assert col.type.modifiers == (10,)


def test_numeric_modifiers_round_trip(schema_and_dsn) -> None:
    from sqlproof.schema.introspect import introspect_schema

    dsn, schema_name = schema_and_dsn
    with psycopg.connect(dsn) as conn, conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
        schema = introspect_schema(cur, schema=schema_name)
    col = schema.table("modifier_types", schema=schema_name).column("price")
    assert col.type.name == "numeric"
    assert col.type.modifiers == (6, 2)


def test_generator_respects_modifiers_end_to_end(schema_and_dsn) -> None:
    """End-to-end: generator must not produce values that overflow column constraints."""
    from sqlproof import SqlProof
    from sqlproof.config import SqlProofConfig

    dsn, schema_name = schema_and_dsn
    proof = SqlProof.from_config(
        SqlProofConfig(connection_string=dsn, schema=schema_name)
    )

    def property_check(db) -> None:
        rows = db.query(
            f'SELECT name, code, price FROM "{schema_name}".modifier_types'
        )
        assert len(rows) == 3
        for row in rows:
            assert len(row["name"]) <= 50
            assert len(row["code"]) == 10
            assert abs(row["price"]) <= Decimal("9999.99")

    proof.check(
        "modifier constraints are respected by the generator",
        sizes={"modifier_types": 3},
        property=property_check,
        runs=5,
    )
