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
function`), shows the default probe cannot see it, and shows that
`nested_plans=True` (auto_explain, scale follow-up 8) does.
"""
from __future__ import annotations

import os

import pytest

from sqlproof import _database as database
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
    with database.connect(os.environ[DSN_ENV], autocommit=True) as connection:
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


def test_a_flip_inside_a_function_is_invisible_by_default(conn):
    """Without nested_plans the probe hashes only the outer `SELECT
    find_one()` plan, a bare Result node at every scale."""
    result = run_sweep(
        conn, _schema(), "flip_test.find_one",
        sizes={"lookups": BASE_ROWS}, max_factor=MAX_FACTOR, min_points=6,
        columns=NEEDLE_COLUMNS,
    )
    assert not result.plan_flips


def test_a_flip_inside_a_function_is_segmented_with_nested_plans(conn):
    """Scale follow-up 8: auto_explain reports the inner query's plan, so
    its flip splits the fit."""
    _require_auto_explain(conn)
    result = run_sweep(
        conn, _schema(), "flip_test.find_one",
        sizes={"lookups": BASE_ROWS}, max_factor=MAX_FACTOR, min_points=6,
        columns=NEEDLE_COLUMNS, nested_plans=True,
    )
    assert result.nested_plans
    assert result.plan_flips, "the inner Index Only Scan -> Bitmap Heap Scan flip was not seen"
    assert len(result.regimes) >= 2


def test_a_sort_inside_a_function_reports_its_memory(conn):
    _require_auto_explain(conn)
    conn.execute(
        "CREATE FUNCTION flip_test.sorted_needles() RETURNS bigint LANGUAGE plpgsql "
        "STABLE AS $$ DECLARE n bigint; BEGIN "
        "SELECT count(*) INTO n FROM (SELECT needle FROM flip_test.lookups "
        "ORDER BY md5(needle::text) OFFSET 0) s; RETURN n; END $$"
    )
    plain = run_sweep(
        conn, _schema(), "flip_test.sorted_needles",
        sizes={"lookups": BASE_ROWS}, max_factor=16,
    )
    nested = run_sweep(
        conn, _schema(), "flip_test.sorted_needles",
        sizes={"lookups": BASE_ROWS}, max_factor=16, nested_plans=True,
    )
    assert all(p.peak_memory_kb == 0 for p in plain.points)
    assert all(p.peak_memory_kb > 0 for p in nested.points)


def _require_auto_explain(conn) -> None:
    """auto_explain needs a superuser, or a server that preloads it and lets
    this role set it (the Supabase images do). Skip, saying so, elsewhere."""
    from sqlproof.exceptions import SqlProofUsageError
    from sqlproof.scale.probe import probe_function

    try:
        probe_function(conn, "pg_catalog.now", [], factor=0, total_rows=0, nested_plans=True)
    except SqlProofUsageError as exc:
        pytest.skip(str(exc))
