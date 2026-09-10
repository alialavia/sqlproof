from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof.scale.probe import probe_function

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE items (id bigint PRIMARY KEY, tag text NOT NULL);
INSERT INTO items SELECT g, 'tag' || (g % 7) FROM generate_series(1, 500) g;
CREATE FUNCTION count_items() RETURNS bigint LANGUAGE plpgsql STABLE AS $$
DECLARE n bigint;
BEGIN SELECT count(*) INTO n FROM items; RETURN n; END $$;
CREATE FUNCTION touch_nothing() RETURNS int LANGUAGE sql IMMUTABLE AS 'SELECT 1';
"""


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS probe_test CASCADE")
        connection.execute("CREATE SCHEMA probe_test")
        connection.execute("SET search_path TO probe_test")
        connection.execute(SCHEMA_SQL)
        try:
            yield connection
        finally:
            connection.execute("DROP SCHEMA IF EXISTS probe_test CASCADE")


def test_probe_measures_work_inside_a_plpgsql_function(conn):
    """The function's internal work must be visible. EXPLAIN on the
    outer call shows a single node, but buffer counters accumulate
    across nested statements into the outer statement's totals."""
    point = probe_function(conn, "count_items", [], factor=1, total_rows=500)
    assert point.work_blocks > 0
    assert point.plan_hash
    assert point.exec_ms >= 0


def test_probe_of_a_function_touching_nothing_reports_near_zero_work(conn):
    point = probe_function(conn, "touch_nothing", [], factor=1, total_rows=0)
    assert point.work_blocks < 50


def test_probe_rolls_back_side_effects(conn):
    """A probe must not mutate the data it is measuring against, or the
    next scale point measures a different database."""
    conn.execute(
        "CREATE FUNCTION probe_test.add_item() RETURNS void LANGUAGE sql AS "
        "$$ INSERT INTO probe_test.items VALUES (999999, 'x') $$"
    )
    before = conn.execute("SELECT count(*) FROM probe_test.items").fetchone()[0]
    probe_function(conn, "probe_test.add_item", [], factor=1, total_rows=500)
    after = conn.execute("SELECT count(*) FROM probe_test.items").fetchone()[0]
    assert after == before


def test_probe_composes_inside_an_existing_transaction(conn):
    """probe_function must not disturb a transaction it did not open.
    DBManager's connection runs with autocommit=False, so it already has
    a transaction active by the time a probe runs, and a caller may have
    uncommitted work in it that must survive the probe untouched -- only
    the probe's own savepoint should roll back."""
    other = psycopg.connect(os.environ[DSN_ENV], autocommit=False)
    try:
        other.execute("SET search_path TO probe_test")
        other.execute("INSERT INTO items VALUES (888888, 'sentinel')")
        point = probe_function(other, "count_items", [], factor=1, total_rows=500)
        assert point.work_blocks > 0
        count = other.execute("SELECT count(*) FROM items").fetchone()[0]
        assert count == 501
    finally:
        other.rollback()
        other.close()


def test_probe_passes_arguments(conn):
    conn.execute(
        "CREATE FUNCTION probe_test.items_with_tag(t text) RETURNS bigint "
        "LANGUAGE sql STABLE AS "
        "$$ SELECT count(*) FROM probe_test.items WHERE tag = t $$"
    )
    point = probe_function(
        conn, "probe_test.items_with_tag", ["tag1"], factor=1, total_rows=500
    )
    assert point.work_blocks > 0
    assert point.args == ("tag1",)
