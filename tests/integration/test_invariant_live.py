"""`SqlProof.invariant()` executes its query on live Postgres (issue #112).

Before #112, `invariant()` always evaluated the query with the in-memory
client, which regex-matched `SELECT <cols> FROM <table>` and ignored
everything else -- even when the proof had a real connection. With a
DSN-backed proof that meant:

  a. a query on a table that does not exist PASSED,
  b. `SELECT id FROM <t> WHERE 1=0` with `expect_empty=True` FAILED,
  c. garbage SQL PASSED.

Each test runs in its own uniquely-named schema (dropped afterwards) so
it cannot collide with other tests sharing the database. The proof's
connection puts that schema on `search_path`, so the queries below are
written unqualified, exactly as in the issue.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from sqlproof import SqlProof
from sqlproof.config import SqlProofConfig
from sqlproof.exceptions import SqlProofPropertyFailure

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)


@pytest.fixture
def live_proof() -> Generator[SqlProof]:
    dsn = os.environ[DSN_ENV]
    schema_name = f"sqlproof_inv_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            connection.execute(
                f"""
                CREATE TABLE "{schema_name}".orders (
                  id INTEGER PRIMARY KEY,
                  total INTEGER NOT NULL CHECK (total >= 0)
                )
                """
            )
            proof_dsn = make_conninfo(dsn, options=f"-csearch_path={schema_name}")
            proof = SqlProof.from_config(
                SqlProofConfig(connection_string=proof_dsn, schema=schema_name)
            )
            try:
                yield proof
            finally:
                proof.disconnect()
        finally:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')


def test_a_query_on_missing_table_raises_instead_of_passing(
    live_proof: SqlProof,
) -> None:
    proof = live_proof
    with pytest.raises(psycopg.errors.UndefinedTable):
        proof.invariant(
            "missing table",
            sizes={"orders": 2},
            query="SELECT id FROM no_such_table",
            runs=1,
        )


def test_b_where_false_with_expect_empty_passes(live_proof: SqlProof) -> None:
    proof = live_proof
    proof.invariant(
        "where false is empty",
        sizes={"orders": 3},
        query="SELECT id FROM orders WHERE 1=0",
        expect_empty=True,
        runs=3,
    )


def test_c_garbage_sql_raises_instead_of_passing(live_proof: SqlProof) -> None:
    proof = live_proof
    with pytest.raises(psycopg.errors.SyntaxError):
        proof.invariant(
            "garbage",
            sizes={"orders": 2},
            query="this is not sql",
            runs=1,
        )


def test_passing_where_invariant(live_proof: SqlProof) -> None:
    # The README example: the CHECK constraint guarantees no negative totals.
    proof = live_proof
    proof.invariant(
        "no negative totals",
        sizes={"orders": 5},
        query="SELECT id FROM orders WHERE total < 0",
        expect_empty=True,
        runs=5,
    )


def test_failing_where_invariant(live_proof: SqlProof) -> None:
    # Every generated row satisfies `total >= 0`, so this always returns rows.
    proof = live_proof
    with pytest.raises(SqlProofPropertyFailure, match="returned 4 rows") as excinfo:
        proof.invariant(
            "no non-negative totals",
            sizes={"orders": 4},
            query="SELECT id FROM orders WHERE total >= 0",
            expect_empty=True,
            runs=1,
        )
    counterexample = excinfo.value.counterexample
    assert counterexample is not None
    assert len(counterexample["dataset"]["orders"]) == 4


def test_expect_non_empty_where_invariant(live_proof: SqlProof) -> None:
    proof = live_proof
    proof.invariant(
        "rows survive the filter",
        sizes={"orders": 2},
        query="SELECT id FROM orders WHERE total >= 0",
        expect_empty=False,
        runs=3,
    )
    with pytest.raises(SqlProofPropertyFailure, match="returned 0 rows"):
        proof.invariant(
            "rows survive an impossible filter",
            sizes={"orders": 2},
            query="SELECT id FROM orders WHERE total < 0",
            expect_empty=False,
            runs=1,
        )


def test_generated_rows_are_rolled_back_between_runs(
    live_proof: SqlProof,
) -> None:
    # If one run's rows leaked into the next, the second run would see
    # 6 rows (and likely hit a primary-key collision first).
    proof = live_proof
    with pytest.raises(SqlProofPropertyFailure, match="returned 3 rows"):
        proof.invariant(
            "rows exist",
            sizes={"orders": 3},
            query="SELECT id FROM orders",
            runs=1,
        )
    proof.invariant(
        "exactly three rows each run",
        sizes={"orders": 3},
        query="SELECT 1 FROM orders HAVING count(*) <> 3",
        runs=5,
    )
    # Same connection/transaction the invariant used, so uncommitted
    # leftovers would be visible here.
    with proof.client_for_dataset({}) as db:
        assert db.scalar("SELECT count(*) FROM orders") == 0


def test_connection_is_usable_after_a_query_error(live_proof: SqlProof) -> None:
    # A failed query aborts the transaction; the savepoint rollback in
    # client_for_dataset must leave the connection usable for the next call.
    proof = live_proof
    with pytest.raises(psycopg.errors.SyntaxError):
        proof.invariant("garbage", sizes={"orders": 1}, query="SELEC nope", runs=1)
    proof.invariant(
        "still works",
        sizes={"orders": 2},
        query="SELECT id FROM orders WHERE total < 0",
        runs=2,
    )
