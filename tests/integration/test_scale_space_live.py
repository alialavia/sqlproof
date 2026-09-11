"""Space behaviour: the cliff, not the curve.

A sort that fits in work_mem is fast; the moment it spills, performance
drops sharply. A fit run straight through that boundary reports a
misleadingly gentle exponent, which is why space is a separate axis
rather than folded into the exponent.

BASE_ROWS=100 was chosen by measurement (see the task-11-12 report),
not guessed: at `work_mem='64kB'`, 100 `wide_rows` (average payload
length ~128 bytes) sort in memory, and the crossover to an external
merge sort falls between roughly 200 and 250 rows. Doubling from a
factor-1 base of 100 puts the first spilling factor at 4 -- mid-ladder,
with two non-spilling points below it and two spilling points above --
rather than at factor 1, where "a spill point exists" would hold even
for a probe that reported temp blocks at every point.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof.exceptions import SqlProofScaleError
from sqlproof.scale.sweep import run_sweep
from sqlproof.schema.parse_sql import parse_schema_sql

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE wide_rows (
  id bigint PRIMARY KEY,
  payload text NOT NULL
);
"""

BASE_ROWS = 100


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        try:
            # Ruling AH: all setup lives inside the try, so a failure
            # partway through still drops whatever schema/objects did
            # get created, instead of leaking them past this test.
            connection.execute("DROP SCHEMA IF EXISTS space_test CASCADE")
            connection.execute("CREATE SCHEMA space_test")
            connection.execute("SET search_path TO space_test")
            connection.execute(SCHEMA_SQL)
            connection.execute(
                "CREATE FUNCTION space_test.sort_everything() RETURNS bigint "
                "LANGUAGE sql STABLE AS $$ "
                "SELECT count(*) FROM (SELECT payload FROM space_test.wide_rows "
                "ORDER BY payload) s $$"
            )
            yield connection
        finally:
            connection.execute("DROP SCHEMA IF EXISTS space_test CASCADE")


def _schema():
    return parse_schema_sql(SCHEMA_SQL, schema="space_test")


def test_a_sort_over_work_mem_reports_a_spill_point(conn):
    # A deliberately tiny work_mem so a modest sort spills. Session
    # scoped, so it cannot leak to other tests.
    conn.execute("SET work_mem = '64kB'")
    result = run_sweep(
        conn, _schema(), "space_test.sort_everything",
        sizes={"wide_rows": BASE_ROWS}, max_factor=16,
    )
    assert result.factor_at_spill is not None, (
        "expected a spill at work_mem=64kB; if this fails, confirm the sort "
        "is actually happening (EXPLAIN should show a Sort node) rather than "
        "relaxing the assertion"
    )
    # Ruling AI: the spill must land mid-ladder, not at factor 1 -- a
    # factor-1 spill would make this assertion pass even for a probe
    # that (wrongly) reported temp blocks at every point.
    assert result.factor_at_spill > 1, (
        f"spilled already at factor 1 -- BASE_ROWS={BASE_ROWS} is too big "
        "for work_mem=64kB to leave a non-spilling point below the spill"
    )
    for point in result.points:
        if point.factor < result.factor_at_spill:
            assert point.temp_blocks == 0, (
                f"factor {point.factor} spilled before the recorded spill "
                f"point (factor {result.factor_at_spill})"
            )
    assert result.spill_point_rows == BASE_ROWS * result.factor_at_spill
    assert result.spills_below(result.spill_point_rows - 1) is False
    assert result.spills_below(result.spill_point_rows) is True


def test_a_sort_that_fits_in_work_mem_reports_no_spill(conn):
    # Ruling AJ: the no-spill case is the SAME sort under a generous
    # work_mem, not a function that never sorts -- otherwise this test
    # would pass even for a probe that (wrongly) never reported a spill
    # at all. Session-scoped; the fixture's connection is discarded
    # after the test.
    conn.execute("SET work_mem = '64MB'")
    result = run_sweep(
        conn, _schema(), "space_test.sort_everything",
        sizes={"wide_rows": BASE_ROWS}, max_factor=16,
    )
    assert result.spill_point_rows is None
    largest = max(point.total_rows for point in result.points)
    assert result.spills_below(largest) is False
    # Ruling AP: at most 1,600 rows were measured, so "no spill at ten
    # million rows" is not an answer this sweep can give.
    with pytest.raises(SqlProofScaleError, match=f"measured up to {largest:,}"):
        result.spills_below(10_000_000)
    assert all(point.temp_blocks == 0 for point in result.points)
