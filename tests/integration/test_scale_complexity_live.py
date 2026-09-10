"""End-to-end complexity recovery against real Postgres.

The functions below are REFERENCE FUNCTIONS -- calibration weights whose
complexity is known by construction, used to check that the fit reports
the right answer. (Not "fixtures": that word already means
`@pytest.fixture` in this suite, and one of those appears below too.)

IMPORTANT when adding one: verify it actually HAS the complexity its
name claims before trusting it as an oracle. Postgres will execute a
naively-written "quadratic" query as a hash join, making it linear --
measured on this project's test database, `SELECT count(*) FROM a, b
WHERE a.x = b.x` fits an exponent of 0.95, not 2. Buffer-visible
quadratic behaviour needs REPEATED SCANS: a procedural FOR loop the
planner cannot rewrite into a join, over a column with no index, re-reads
the table once per outer row, and every re-read touches its pages again.
That shape fits 1.95, which is why `quadratic_fn` below is written the
way it is.

Buffers count I/O-visible work, not CPU. A quadratic that re-reads
nothing is INVISIBLE to the fit: `cpu_quadratic_fn`'s non-equi self-join
runs as a Nested Loop over a Materialize, which scans the table once and
replays it from memory for every outer row -- O(n^2) comparisons on O(n)
buffer work, so it fits ~1. The other gap runs the opposite way: an O(1)
function whose work moves by a block or two from point to point, like
`pk_lookup_fn`, has R^2 near 0 by construction and is refused rather
than fitted ~0. Both are pinned below as strict xfails (Rulings AS, AT),
so neither gap can close -- or stay open -- silently.

A wrong oracle is worse than no oracle: it reads as a broken fit, and
the tempting repair -- widening the tolerance until it passes -- leaves
the measurement validated against something that proves nothing.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof.exceptions import SqlProofScaleError
from sqlproof.scale.args import heaviest
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
CREATE TABLE items (id bigint PRIMARY KEY, v bigint NOT NULL, name text NOT NULL);
"""

# No index on events.org_id, so the per-org lookup is a full scan. No
# index on items.v either, so the non-equi join cannot use one.
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

CREATE FUNCTION scale_test.cpu_quadratic_fn() RETURNS bigint LANGUAGE sql STABLE AS
$$ SELECT count(*) FROM scale_test.items a JOIN scale_test.items b ON a.v < b.v $$;

CREATE FUNCTION scale_test.pk_lookup_fn(k bigint) RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE r text;
BEGIN SELECT name INTO r FROM scale_test.items WHERE id = k; RETURN r; END $$;
"""


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS scale_test CASCADE")
        try:
            # Ruling W: setup lives inside the try too, so a failure partway
            # through (e.g. FUNCTIONS_SQL) still drops whatever schema/tables
            # did get created, instead of leaking them past this test.
            connection.execute("CREATE SCHEMA scale_test")
            connection.execute("SET search_path TO scale_test")
            connection.execute(SCHEMA_SQL)
            connection.execute(FUNCTIONS_SQL)
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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "C2 / Ruling AS: buffers measure I/O-visible work, not CPU. "
        "cpu_quadratic_fn's non-equi self-join runs as a Nested Loop over a "
        "Materialize that replays the inner side from memory, so its buffer "
        "work grows ~n while its CPU work grows ~n^2: the final review "
        "measured an exponent of ~1.04 (R^2 0.9998) while wall-clock grew "
        "~n^1.84. The gate passes a real quadratic. strict=True: the day the "
        "gate can see CPU-only growth, this XPASSes and fails, so the blind "
        "spot cannot close -- or stay open -- silently."
    ),
)
def test_a_cpu_only_quadratic_recovers_exponent_near_two(conn):
    result = run_sweep(
        conn, _schema(), "scale_test.cpu_quadratic_fn",
        sizes={"items": 200}, max_factor=16,
    )
    assert result.exponent > 1.6, (
        f"fitted {result.exponent} on buffer work; the non-equi join's O(n^2) "
        "comparisons touch no buffers after the first scan"
    )


@pytest.mark.xfail(
    strict=True,
    raises=SqlProofScaleError,
    reason=(
        "I1 / Ruling AT: flat-but-noisy work is refused. pk_lookup_fn is a "
        "primary-key lookup, O(1), yet its buffer work moves by a block or "
        "two from point to point with no trend (the final review saw 9-13 "
        "blocks against a calibrated baseline of 4); against a flat series "
        "R^2 is near 0 by construction -- 0.000 on this suite's data -- below "
        "the spec's MIN_R_SQUARED of 0.98, so "
        ".exponent raises instead of reporting ~0 -- `assert exponent < 1.5` "
        "fails CI for a well-behaved function. strict=True: the day the fit "
        "accepts a confidently flat series (e.g. a slope-standard-error "
        "gate), this XPASSes and fails, so the gap cannot close -- or stay "
        "open -- silently."
    ),
)
def test_a_primary_key_lookup_recovers_exponent_near_zero(conn):
    result = run_sweep(
        conn, _schema(), "scale_test.pk_lookup_fn",
        sizes={"items": 1000}, args=[heaviest("scale_test.items.id")], max_factor=16,
    )
    assert abs(result.exponent) < 0.4
