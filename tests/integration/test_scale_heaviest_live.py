"""`heaviest` finds the most-referenced parent (scale follow-up 7).

Loaded with `distribution="zipf"`, a few customers own most invoices; the
resolver must agree with a direct count, across two referencing tables.
"""

from __future__ import annotations

import os

import pytest

from sqlproof import _database as database
from sqlproof.scale.args import heaviest
from sqlproof.scale.load import load_dataset
from sqlproof.schema.introspect import introspect_schema

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"
SCHEMA = "scale_heaviest"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)


@pytest.fixture
def conn():
    dsn = os.environ[DSN_ENV]
    with database.connect(dsn, autocommit=True) as connection:
        connection.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        connection.execute(f"CREATE SCHEMA {SCHEMA}")
        connection.execute(
            f"CREATE TABLE {SCHEMA}.customers (id bigint PRIMARY KEY);"
            f"CREATE TABLE {SCHEMA}.invoices ("
            f"  id bigint PRIMARY KEY,"
            f"  customer_id bigint NOT NULL REFERENCES {SCHEMA}.customers(id));"
            f"CREATE TABLE {SCHEMA}.notes ("
            f"  id bigint PRIMARY KEY,"
            f"  about bigint REFERENCES {SCHEMA}.customers(id));"
        )
        try:
            yield connection
        finally:
            connection.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def test_heaviest_is_the_most_referenced_customer_under_zipf(conn):
    with database.connect(os.environ[DSN_ENV], row_factory=database.dict_row) as c:
        schema = introspect_schema(c, schema=SCHEMA)
    load_dataset(
        conn,
        schema,
        {"customers": 50, "invoices": 2000, "notes": 300},
        seed=3,
        distribution="zipf",
    )
    expected = conn.execute(
        f"SELECT k FROM ("
        f"  SELECT customer_id AS k FROM {SCHEMA}.invoices"
        f"  UNION ALL SELECT about FROM {SCHEMA}.notes WHERE about IS NOT NULL"
        f") r GROUP BY k ORDER BY count(*) DESC, k DESC LIMIT 1"
    ).fetchone()[0]
    assert heaviest(f"{SCHEMA}.customers.id")(conn) == expected
    # Under zipf that is not the largest key, which the old resolver chose.
    assert expected != 50


def test_an_unreferenced_column_falls_back_to_the_largest_key(conn):
    with database.connect(os.environ[DSN_ENV], row_factory=database.dict_row) as c:
        schema = introspect_schema(c, schema=SCHEMA)
    load_dataset(conn, schema, {"customers": 5, "invoices": 20, "notes": 0}, seed=1)
    assert heaviest(f"{SCHEMA}.invoices.id")(conn) == 20
