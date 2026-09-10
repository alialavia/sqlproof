"""End-to-end complexity recovery against real Postgres.

The functions below are REFERENCE FUNCTIONS -- calibration weights whose
complexity is known by construction, used to check that the fit reports
the right answer. (Not "fixtures": that word already means
`@pytest.fixture` in this suite, and one of those appears below too.)

IMPORTANT when adding one: verify it actually HAS the complexity its
name claims before trusting it as an oracle. Postgres will execute a
naively-written "quadratic" query as a hash join, making it linear --
measured on this project's test database, `SELECT count(*) FROM a, b
WHERE a.x = b.x` fits an exponent of 0.95, not 2. Genuine quadratic
behaviour needs a procedural FOR loop the planner cannot rewrite into a
join, over a column with no index; that shape fits 1.95, which is why
`quadratic_fn` below is written the way it is.

A wrong oracle is worse than no oracle: it reads as a broken fit, and
the tempting repair -- widening the tolerance until it passes -- leaves
the measurement validated against something that proves nothing.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof.scale.sweep import run_sweep
from sqlproof.schema.parse_sql import parse_schema_sql

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE orgs (id bigint PRIMARY KEY, name text NOT NULL);
CREATE TABLE events (
  id bigint PRIMARY KEY,
  org_id bigint NOT NULL REFERENCES orgs(id),
  tag text NOT NULL
);
"""

# No index on events.org_id, so the per-org lookup is a full scan.
FUNCTIONS_SQL = """
CREATE FUNCTION scale_test.linear_fn() RETURNS bigint LANGUAGE plpgsql STABLE AS $$
DECLARE n bigint;
BEGIN SELECT count(*) INTO n FROM scale_test.events; RETURN n; END $$;

CREATE FUNCTION scale_test.quadratic_fn() RETURNS bigint LANGUAGE plpgsql STABLE AS $$
DECLARE o record; total bigint := 0; c bigint;
BEGIN
  FOR o IN SELECT id FROM scale_test.orgs LOOP
    SELECT count(*) INTO c FROM scale_test.events WHERE org_id = o.id;
    total := total + c;
  END LOOP;
  RETURN total;
END $$;

CREATE FUNCTION scale_test.constant_fn() RETURNS int LANGUAGE sql IMMUTABLE AS
  'SELECT 1';
"""


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS scale_test CASCADE")
        connection.execute("CREATE SCHEMA scale_test")
        connection.execute("SET search_path TO scale_test")
        connection.execute(SCHEMA_SQL)
        connection.execute(FUNCTIONS_SQL)
        try:
            yield connection
        finally:
            connection.execute("DROP SCHEMA IF EXISTS scale_test CASCADE")


def _schema():
    return parse_schema_sql(SCHEMA_SQL, schema="scale_test")


def test_linear_function_recovers_exponent_near_one(conn):
    result = run_sweep(
        conn, _schema(), "scale_test.linear_fn",
        sizes={"orgs": 20, "events": 400}, max_factor=16,
    )
    assert 0.7 < result.exponent < 1.3


def test_quadratic_function_recovers_exponent_near_two(conn):
    result = run_sweep(
        conn, _schema(), "scale_test.quadratic_fn",
        sizes={"orgs": 20, "events": 400}, max_factor=16,
    )
    assert 1.6 < result.exponent < 2.4


def test_constant_function_recovers_exponent_near_zero(conn):
    result = run_sweep(
        conn, _schema(), "scale_test.constant_fn",
        sizes={"orgs": 20, "events": 400}, max_factor=16,
    )
    assert abs(result.exponent) < 0.4


def test_sweep_records_every_point_it_measured(conn):
    result = run_sweep(
        conn, _schema(), "scale_test.linear_fn",
        sizes={"orgs": 20, "events": 400}, max_factor=16,
    )
    assert len(result.points) >= 5
    assert [p.factor for p in result.points] == sorted(p.factor for p in result.points)
    assert all(p.total_rows == 420 * p.factor for p in result.points)
