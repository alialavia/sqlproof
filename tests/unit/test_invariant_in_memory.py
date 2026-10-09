"""`SqlProof.invariant()` without a database connection (issue #112).

The in-memory client only understands the projection and table name of
a ``SELECT``. Before #112 it silently ignored everything else, so a
WHERE-filtered invariant degraded to "does the table have rows", and
garbage SQL or an unknown table passed. Queries it cannot evaluate
faithfully must now be refused up front with a usage error that points
at a database connection, never reported as a pass or a fail.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlproof import SqlProof
from sqlproof.exceptions import SqlProofPropertyFailure, SqlProofUsageError

SCHEMA_SQL = """
CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  total INTEGER NOT NULL CHECK (total >= 0)
);
"""


@pytest.fixture
def proof(tmp_path: Path) -> SqlProof:
    schema_file = tmp_path / "schema.sql"
    schema_file.write_text(SCHEMA_SQL, encoding="utf-8")
    return SqlProof.from_schema_file(schema_file)


@pytest.mark.parametrize(
    "query",
    [
        # The README example: the WHERE clause used to be dropped.
        "SELECT id FROM orders WHERE total < 0",
        "SELECT id FROM orders WHERE 1=0",
        "SELECT o.id FROM orders o JOIN orders p ON p.id = o.id",
        "SELECT total FROM orders GROUP BY total HAVING count(*) > 1",
        "SELECT DISTINCT total FROM orders",
        "SELECT count(*) FROM orders",
        "SELECT id FROM orders ORDER BY id LIMIT 1",
        "SELECT id FROM orders; DELETE FROM orders",
        "this is not sql",
        "",
    ],
)
def test_in_memory_invariant_refuses_queries_it_cannot_evaluate(
    proof: SqlProof, query: str
) -> None:
    with pytest.raises(SqlProofUsageError, match="database connection"):
        proof.invariant("refused", sizes={"orders": 2}, query=query, runs=1)


def test_in_memory_invariant_refuses_unknown_table(proof: SqlProof) -> None:
    with pytest.raises(SqlProofUsageError, match=r"'missing_table'.*not in the schema"):
        proof.invariant(
            "missing table",
            sizes={"orders": 2},
            query="SELECT id FROM missing_table",
            runs=1,
        )


def test_in_memory_invariant_refuses_unknown_column(proof: SqlProof) -> None:
    with pytest.raises(SqlProofUsageError, match="'nope'"):
        proof.invariant(
            "missing column",
            sizes={"orders": 2},
            query="SELECT nope FROM orders",
            runs=1,
        )


def test_in_memory_invariant_refuses_foreign_qualifier(proof: SqlProof) -> None:
    with pytest.raises(SqlProofUsageError, match="qualifies a column"):
        proof.invariant(
            "bad qualifier",
            sizes={"orders": 2},
            query="SELECT other.id FROM orders",
            runs=1,
        )


def test_refusal_happens_before_any_dataset_is_drawn(
    proof: SqlProof, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlproof import core

    def fail_if_called(*args: object, **kwargs: object) -> object:
        raise AssertionError("dataset should not be drawn for a refused query")

    monkeypatch.setattr(core, "draw_example", fail_if_called)
    with pytest.raises(SqlProofUsageError):
        proof.invariant(
            "refused",
            sizes={"orders": 2},
            query="SELECT id FROM orders WHERE total < 0",
            runs=3,
        )


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM orders",
        "select * from orders;",
        'SELECT orders.id, "total" FROM "orders"',
        "SELECT ID, Orders.Total FROM Orders",
    ],
)
def test_in_memory_plain_select_still_evaluates(proof: SqlProof, query: str) -> None:
    # Non-empty table: the plain projection returns every row.
    with pytest.raises(SqlProofPropertyFailure, match="returned 2 rows"):
        proof.invariant("rows exist", sizes={"orders": 2}, query=query, runs=1)
    proof.invariant("rows exist", sizes={"orders": 2}, query=query, expect_empty=False, runs=2)
    # Empty table: nothing to return.
    proof.invariant("no rows", sizes={"orders": 0}, query=query, runs=2)
    with pytest.raises(SqlProofPropertyFailure, match="returned 0 rows"):
        proof.invariant("no rows", sizes={"orders": 0}, query=query, expect_empty=False, runs=1)
