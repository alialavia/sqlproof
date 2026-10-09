"""Plan flips are findings, not noise -- when the probe can see them.

At small n a low-cardinality index lookup returns few enough rows that
the planner chooses `Index Only Scan`; past some size, enough rows
match that it switches to `Bitmap Heap Scan`. That is a real
discontinuity in the cost curve.

Known gap, proved rather than asserted away: `probe_function` measures
`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT fn(...)`. `find_one` is
`LANGUAGE sql` with a FROM clause and an aggregate, so Postgres does
not inline it -- the OUTER plan tree is always a single `Result` node,
at every scale, regardless of what the statement inside the function
does. Statements executed inside a function are invisible to EXPLAIN of
the wrapping call, so `plan_hash` (derived from that outer tree) never
changes even when the inner query's plan genuinely does. This module
proves the inner flip is real (`test_the_planner_flips_inside_the_
function`), then pins the probe's inability to see it as a strict
xfail (`test_a_flip_inside_a_function_is_segmented`): the day nested
plans become visible to the probe, that test XPASSes and fails loudly,
so the gap cannot close -- or stay open -- silently.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof.scale.load import analyze, load_dataset, truncate
from sqlproof.scale.sweep import run_sweep
from sqlproof.schema.parse_sql import parse_schema_sql

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE lookups (
  id bigint PRIMARY KEY,
  needle bigint NOT NULL
);
"""

BASE_ROWS = 500
MAX_FACTOR = 32

# The bulk generator's default `needle` sampler draws uniformly across
# the full bigint range (measured: `needle = 1` then matches 0 rows at
# every ladder point from 500 to 16,000, and the inner query's plan
# never changes -- always `Index Only Scan`; see the task report). A
# `row_index % 1000` override instead makes `needle = 1` match
# `total_rows // 1000` rows: 0-1 matches at the two smallest ladder
# points (500, 1000 rows) and 2+ at the rest (2000-16,000 rows), which
# measured crosses the planner's Index-Only-Scan / Bitmap-Heap-Scan
# boundary within this sweep's own range. Chosen by measurement, not
# guessed -- see the task-11-12 report for the ladder of tries.
NEEDLE_COLUMNS = {"lookups.needle": lambda ctx: ctx.row_index % 1000}


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        try:
            # Ruling AH: all setup lives inside the try, so a failure
            # partway through still drops whatever schema/objects did
            # get created, instead of leaking them past this test.
            connection.execute("DROP SCHEMA IF EXISTS flip_test CASCADE")
            connection.execute("CREATE SCHEMA flip_test")
            connection.execute("SET search_path TO flip_test")
            connection.execute(SCHEMA_SQL)
            connection.execute("CREATE INDEX ON flip_test.lookups (needle)")
            connection.execute(
                "CREATE FUNCTION flip_test.find_one() RETURNS bigint LANGUAGE sql "
                "STABLE AS $$ SELECT count(*) FROM flip_test.lookups "
                "WHERE needle = 1 $$"
            )
            yield connection
        finally:
            connection.execute("DROP SCHEMA IF EXISTS flip_test CASCADE")


def _schema():
    return parse_schema_sql(SCHEMA_SQL, schema="flip_test")


def _node_shape(node: dict) -> str:
    """Node types and nesting only -- mirrors `scale/probe.py`'s
    `_shape`, applied here to the INNER query's own plan rather than
    the outer `SELECT fn()` wrapper."""
    children = node.get("Plans", [])
    if not children:
        return str(node["Node Type"])
    return f"{node['Node Type']}(" + ",".join(_node_shape(c) for c in children) + ")"


def _inner_plan_shape(conn) -> str:
    plan = conn.execute(
        "EXPLAIN (FORMAT JSON) SELECT count(*) FROM flip_test.lookups "
        "WHERE needle = 1"
    ).fetchone()[0][0]["Plan"]
    return _node_shape(plan)


def test_the_planner_flips_inside_the_function(conn):
    """Proves the scenario is real, independent of the probe: at the
    smallest and largest size the sweep below would reach, the INNER
    query's own plan (not the outer `SELECT find_one()` wrapper) changes
    shape."""
    schema = _schema()
    truncate(conn, schema)
    load_dataset(conn, schema, {"lookups": BASE_ROWS}, seed=0, columns=NEEDLE_COLUMNS)
    analyze(conn, schema)
    smallest = _inner_plan_shape(conn)

    truncate(conn, schema)
    load_dataset(
        conn, schema, {"lookups": BASE_ROWS * MAX_FACTOR}, seed=0,
        columns=NEEDLE_COLUMNS,
    )
    analyze(conn, schema)
    largest = _inner_plan_shape(conn)

    assert smallest != largest, (
        f"expected the inner plan to change shape between {BASE_ROWS} and "
        f"{BASE_ROWS * MAX_FACTOR} rows; got {smallest!r} at both ends -- "
        "pick a different needle distribution or profile rather than "
        "weakening this assertion"
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "probe_function measures EXPLAIN of the OUTER `SELECT find_one()` "
        "call; find_one is LANGUAGE sql and not inlined, so that tree is "
        "always a bare Result node regardless of scale. Plans of "
        "statements inside a function are invisible to EXPLAIN, so an "
        "inner plan flip (proved real by "
        "test_the_planner_flips_inside_the_function) cannot be segmented "
        "yet. strict=True is the point: the day someone makes nested "
        "plans visible to the probe, this XPASSes and fails, so the gap "
        "cannot close -- or stay open -- silently."
    ),
)
def test_a_flip_inside_a_function_is_segmented(conn):
    schema = _schema()
    result = run_sweep(
        conn, schema, "flip_test.find_one",
        sizes={"lookups": BASE_ROWS}, max_factor=MAX_FACTOR, min_points=6,
        columns=NEEDLE_COLUMNS,
    )
    assert result.plan_flips, (
        "no plan flip recorded: the probe hashes only the outer SELECT "
        "find_one() plan, so it cannot see the inner query's plan change"
    )
    assert len(result.regimes) >= 2
