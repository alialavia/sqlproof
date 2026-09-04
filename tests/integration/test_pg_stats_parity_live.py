"""pg_stats is the planner's own input, which makes it the right
equivalence oracle for a feature whose output is a measurement.

If the bulk path never produces NULLs where the Hypothesis path does,
selectivity shifts, the planner may choose a different plan, and the
sweep measures something users never run.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof.core import _insert_dataset
from sqlproof.generators.graph import dataset_strategy
from sqlproof.generators.sampling import draw_example
from sqlproof.scale.load import analyze, load_dataset
from sqlproof.schema.parse_sql import parse_schema_sql

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE items (
  id bigint PRIMARY KEY,
  label text,
  qty integer NOT NULL
);
"""
N = 800


def _stats(conn, schema_name: str) -> dict[str, tuple[float, float]]:
    rows = conn.execute(
        "SELECT attname, null_frac, n_distinct FROM pg_stats "
        "WHERE schemaname = %s AND tablename = 'items'",
        (schema_name,),
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        yield connection


def _prepare(conn, name: str) -> None:
    conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
    conn.execute(f"CREATE SCHEMA {name}")
    conn.execute(f"SET search_path TO {name}")
    conn.execute(SCHEMA_SQL)


def test_null_fraction_is_comparable_between_paths(conn):
    schema_h = parse_schema_sql(SCHEMA_SQL, schema="parity_h")
    schema_b = parse_schema_sql(SCHEMA_SQL, schema="parity_b")
    try:
        _prepare(conn, "parity_h")
        dataset = draw_example(dataset_strategy(schema_h, sizes={"items": N}))
        _insert_dataset(_ConnClient(conn), schema_h, dataset)
        analyze(conn, schema_h)
        hypothesis_stats = _stats(conn, "parity_h")

        _prepare(conn, "parity_b")
        load_dataset(conn, schema_b, {"items": N}, seed=1)
        analyze(conn, schema_b)
        bulk_stats = _stats(conn, "parity_b")

        h_null = hypothesis_stats["label"][0]
        b_null = bulk_stats["label"][0]
        # Tolerance is generous; the point is to catch "bulk never emits
        # NULL" (0.0 vs 0.5), not to force the paths to match exactly.
        assert abs(h_null - b_null) < 0.20, (
            f"null_frac diverged: hypothesis={h_null}, bulk={b_null}. "
            "Tune DEFAULT_NULL_FRAC in generators/bulk.py."
        )
    finally:
        conn.execute("DROP SCHEMA IF EXISTS parity_h CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS parity_b CASCADE")


class _ConnClient:
    """Minimal SqlProofClient adapter so _insert_dataset can run on a
    raw psycopg connection."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql: str, *params):
        self._conn.execute(sql, params or None)
        return 0
