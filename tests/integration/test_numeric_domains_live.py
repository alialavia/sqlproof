"""PostgreSQL is the oracle: generated numeric values must store unchanged."""

from __future__ import annotations

import os
import random
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sqlproof import SqlProof
from sqlproof import _database as database
from sqlproof.config import SqlProofConfig
from sqlproof.generators.bulk import bulk_table_rows
from sqlproof.scale.load import copy_table
from sqlproof.schema.introspect import introspect_schema
from sqlproof.schema.parse_sql import parse_schema_sql

pytestmark = pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE DOMAIN rounded_amount AS numeric(2, -3) CHECK (VALUE >= -99000 AND VALUE <= 99000);
CREATE DOMAIN rounded_list AS numeric(2, -3)[];
CREATE TABLE amounts (
    id integer PRIMARY KEY,
    whole numeric(2) NOT NULL,
    thousands numeric(2, -3) NOT NULL,
    fraction numeric(3, 5) NOT NULL,
    wide numeric(40, 2) NOT NULL,
    large numeric(1000) NOT NULL,
    far numeric(2, -1000) NOT NULL,
    fine numeric(2, 1000) NOT NULL,
    rounded rounded_amount NOT NULL,
    items numeric(2, -3)[] NOT NULL,
    domain_items rounded_amount[] NOT NULL,
    list_domain rounded_list NOT NULL,
    positive_edge numeric(2, -3) NOT NULL CHECK (positive_edge > 1001 AND positive_edge < 2999),
    negative_edge numeric(2, -3) NOT NULL CHECK (negative_edge > -2999 AND negative_edge < -1001),
    cents numeric(3, 2) NOT NULL CHECK (cents >= 0.001 AND cents <= 0.019)
);
"""


@pytest.fixture
def numeric_schema():
    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    name = f"sqlproof_numeric_{uuid4().hex}"
    with database.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
        try:
            conn.execute(f'SET search_path TO "{name}"')
            conn.execute(SCHEMA_SQL)
            yield dsn, name, conn
        finally:
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')


def test_negative_scales_round_trip_through_columns_arrays_and_domains(numeric_schema):
    _, name, conn = numeric_schema
    with conn.cursor(row_factory=database.dict_row) as cur:
        schema = introspect_schema(cur, schema=name)
    table = schema.table("amounts", schema=name)
    assert table.column("thousands").type.modifiers == (2, -3)
    assert table.column("items").type.base.modifiers == (2, -3)
    assert table.column("rounded").type.base.modifiers == (2, -3)
    assert table.column("domain_items").type.base.base.modifiers == (2, -3)
    assert table.column("list_domain").type.base.base.modifiers == (2, -3)
    assert table.column("far").type.modifiers == (2, -1000)
    assert table.column("fine").type.modifiers == (2, 1000)


@pytest.mark.parametrize("source", ["sql", "database"])
@pytest.mark.parametrize("path", ["hypothesis", "bulk"])
def test_numeric_values_store_without_overflow_or_rounding(numeric_schema, source, path):
    """Oracle: INSERT/COPY and read back every value, including sub-cent CHECKs."""
    dsn, name, conn = numeric_schema
    proof = SqlProof.from_config(SqlProofConfig(connection_string=dsn, schema=name))
    if source == "sql":
        proof.schema_info = parse_schema_sql(SCHEMA_SQL, schema=name)
    table = proof.schema_info.table("amounts", schema=name)
    # Read domain elements as numeric, so this checks stored values without
    # depending on the driver's custom-domain array decoding.
    columns = ", ".join(
        'domain_items::numeric[] AS domain_items' if col.name == "domain_items" else f'"{col.name}"'
        for col in table.columns
    )
    select = f'SELECT {columns} FROM "{name}".amounts ORDER BY id'

    def assert_unchanged(stored, rows):
        assert len(stored) == len(rows)
        for actual, expected in zip(stored, rows, strict=True):
            for column in table.columns:
                assert actual[column.name] == expected[column.name], column.name

    if path == "bulk":
        rows = list(bulk_table_rows(table, count=100, rng=random.Random(42), parent_counts={}))
        copy_table(conn, table, rows, [col.name for col in table.columns])
        with conn.cursor(row_factory=database.dict_row) as cur:
            stored = cur.execute(select).fetchall()
        assert_unchanged(stored, rows)
    else:
        @settings(max_examples=20, deadline=None, database=None)
        @given(data=st.data())
        def check(data):
            dataset = data.draw(proof.dataset_strategy(sizes={"amounts": 3}))
            with proof.client_for_dataset(dataset) as client:
                stored = client.query(select)
            assert_unchanged(stored, dataset["amounts"])

        check()
