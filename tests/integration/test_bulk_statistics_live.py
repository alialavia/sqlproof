"""Planner statistics over bulk data match the shared windows.

The six unbounded kinds draw from one window shared by both generators
(see typespec.py). Bulk data exists so planner statistics mean
something; this checks that they say what the windows predict, through
the planner's own `pg_stats` after a real `COPY` and `ANALYZE`:

- `n_distinct` matches the expected number of distinct values for `N`
  uniform draws from the window's `K` values,
  `K * (1 - (1 - 1/K) ** N)`. A window that silently changed size (say,
  dates collapsing to one year) moves this far outside the tolerance.
- `correlation` is near zero: rows are generated independently, so
  physical order says nothing about value order. A generator that drew
  increasing values would show up here as a correlation near 1.

The comparison is with analytic expectations, not with Hypothesis-drawn
data: see tests/unit/test_shared_windows.py for why a cross-path
`pg_stats` comparison is ill-posed, and for the value-level agreement
between the two paths.
"""

from __future__ import annotations

import os

import pytest

from sqlproof import _database as database
from sqlproof.generators.typespec import DATE_WINDOW, DATETIME_WINDOW, INTERVAL_WINDOW
from sqlproof.scale.load import analyze, load_dataset
from sqlproof.schema.parse_sql import parse_schema_sql

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_NAME = "bulk_statistics"
SCHEMA_SQL = """
CREATE TABLE samples (
  id bigint PRIMARY KEY,
  on_day date NOT NULL,
  at_time timestamp NOT NULL,
  waited interval NOT NULL,
  score double precision NOT NULL
);
"""
# Below ANALYZE's default 30,000-row sample, so the statistics are
# computed over every row rather than estimated from a sample.
N = 5000


def _expected_distinct(k: int) -> float:
    return k * (1 - (1 - 1 / k) ** N)


EXPECTED_DISTINCT = {
    "on_day": _expected_distinct((DATE_WINDOW[1] - DATE_WINDOW[0]).days + 1),
    "at_time": _expected_distinct(
        int((DATETIME_WINDOW[1] - DATETIME_WINDOW[0]).total_seconds()) + 1
    ),
    "waited": _expected_distinct(
        int((INTERVAL_WINDOW[1] - INTERVAL_WINDOW[0]).total_seconds()) + 1
    ),
    "score": float(N),  # continuous: every draw distinct
}


@pytest.fixture(scope="module")
def stats() -> dict[str, tuple[float, float]]:
    with database.connect(os.environ[DSN_ENV], autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE")
        conn.execute(f"CREATE SCHEMA {SCHEMA_NAME}")
        conn.execute(f"SET search_path TO {SCHEMA_NAME}")
        conn.execute(SCHEMA_SQL)
        try:
            schema = parse_schema_sql(SCHEMA_SQL, schema=SCHEMA_NAME)
            load_dataset(conn, schema, {"samples": N}, seed=3)
            analyze(conn, schema)
            rows = conn.execute(
                "SELECT attname, n_distinct, correlation FROM pg_stats "
                "WHERE schemaname = %s AND tablename = 'samples'",
                (SCHEMA_NAME,),
            ).fetchall()
        finally:
            conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA_NAME} CASCADE")
    return {str(name): (float(nd), float(corr)) for name, nd, corr in rows}


@pytest.mark.parametrize("column", sorted(EXPECTED_DISTINCT))
def test_n_distinct_matches_the_window(stats: dict[str, tuple[float, float]], column: str) -> None:
    n_distinct, _ = stats[column]
    # Negative n_distinct is a fraction of the row count.
    observed = -n_distinct * N if n_distinct < 0 else n_distinct
    expected = EXPECTED_DISTINCT[column]
    # The binomial spread of the distinct count is under 1% of N here;
    # 3% catches a window that changed size without flaking.
    assert abs(observed - expected) <= 0.03 * N, (column, observed, expected)


@pytest.mark.parametrize("column", sorted(EXPECTED_DISTINCT))
def test_correlation_is_near_zero(stats: dict[str, tuple[float, float]], column: str) -> None:
    _, correlation = stats[column]
    # Independent draws: standard error about 1/sqrt(N) = 0.014.
    assert abs(correlation) < 0.1, (column, correlation)
