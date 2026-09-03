"""Postgres is the oracle for validity.

We never assert the two generation paths agree with each other about
what is valid -- they could be identically wrong. We assert each one
independently against a real database: generate, load with every
constraint enabled, and let Postgres reject anything invalid.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.rows import dict_row

from sqlproof.exceptions import SqlProofGenerationError
from sqlproof.scale.load import analyze, load_dataset
from sqlproof.schema.introspect import introspect_schema
from sqlproof.schema.parse_sql import parse_schema_sql

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE customers (
  id bigint PRIMARY KEY,
  email text NOT NULL,
  tier text
);
CREATE TABLE orders (
  id bigint PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES customers(id),
  total numeric(10,2) NOT NULL CHECK (total >= 0),
  placed_at timestamp NOT NULL
);
"""


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS bulk_test CASCADE")
        connection.execute("CREATE SCHEMA bulk_test")
        connection.execute("SET search_path TO bulk_test")
        connection.execute(SCHEMA_SQL)
        yield connection
        connection.execute("DROP SCHEMA IF EXISTS bulk_test CASCADE")


def test_bulk_load_satisfies_every_constraint(conn):
    schema = parse_schema_sql(SCHEMA_SQL, schema="bulk_test")
    counts = load_dataset(conn, schema, {"customers": 200, "orders": 2000}, seed=1)
    assert counts == {"customers": 200, "orders": 2000}
    assert conn.execute("SELECT count(*) FROM bulk_test.orders").fetchone()[0] == 2000
    # If any FK, CHECK or NOT NULL were violated, COPY would have raised.
    orphans = conn.execute(
        "SELECT count(*) FROM bulk_test.orders o "
        "LEFT JOIN bulk_test.customers c ON c.id = o.customer_id "
        "WHERE c.id IS NULL"
    ).fetchone()[0]
    assert orphans == 0


def test_analyze_populates_planner_statistics(conn):
    schema = parse_schema_sql(SCHEMA_SQL, schema="bulk_test")
    load_dataset(conn, schema, {"customers": 100, "orders": 1000}, seed=1)
    analyze(conn, schema)
    rows = conn.execute(
        "SELECT count(*) FROM pg_stats WHERE schemaname = 'bulk_test'"
    ).fetchone()[0]
    assert rows > 0


def test_cyclic_schema_raises_rather_than_loading_null_relationships(conn):
    """The dangerous failure, made loud.

    Without the guard this load SUCCEEDS with every deferred FK NULL,
    and a later sweep would measure a function against data whose
    relationships do not exist -- returning a confident wrong answer.

    The `SchemaInfo` here comes from live introspection, not
    `parse_schema_sql`: `parse_schema_sql` silently drops `ALTER
    TABLE ... ADD CONSTRAINT ... FOREIGN KEY` (confirmed -- on this
    exact cyclic SQL it returns `content.foreign_keys == ()`), which
    would make the cycle invisible to `resolve_insertion_plan` and
    fail this test for the wrong reason (either no raise at all, or a
    `psycopg` error from the tables never having been introspected
    correctly, instead of the `SqlProofGenerationError` this test is
    actually about). Introspecting the live schema after applying the
    ALTER TABLE picks the FK up the same way a real caller's schema
    would be read.
    """
    cyclic = """
    CREATE TABLE content (id bigint PRIMARY KEY, current_version_id bigint);
    CREATE TABLE versions (
      id bigint PRIMARY KEY,
      content_id bigint NOT NULL REFERENCES content(id)
    );
    ALTER TABLE content ADD CONSTRAINT fk_cv
      FOREIGN KEY (current_version_id) REFERENCES versions(id);
    """
    conn.execute(cyclic)

    with psycopg.connect(
        os.environ[DSN_ENV], autocommit=True, row_factory=dict_row
    ) as introspect_conn:
        schema = introspect_schema(introspect_conn, schema="bulk_test")

    with pytest.raises(SqlProofGenerationError, match="foreign-key cycles"):
        load_dataset(conn, schema, {"content": 10, "versions": 10}, seed=1)


def test_load_is_linear_enough_to_reach_scale(conn):
    """50k rows must load in seconds, not the hours the Hypothesis path
    would take (projected ~3h at 500k -- see the design doc's Evidence)."""
    import time

    schema = parse_schema_sql(SCHEMA_SQL, schema="bulk_test")
    start = time.perf_counter()
    load_dataset(conn, schema, {"customers": 500, "orders": 50_000}, seed=1)
    assert time.perf_counter() - start < 60
