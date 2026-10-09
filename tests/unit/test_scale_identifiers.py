"""The probed function's name is interpolated into SQL, so it is checked
before any SQL runs (Ruling AN). No database: a fake connection records
every statement it is handed, so "refused before any SQL" is observable.

The reviewer's payload below, unchecked, builds `EXPLAIN ... SELECT
rv.noop(); COMMIT; DELETE FROM rv.canary; SELECT rv.noop()` -- the
simple-query protocol runs all four statements, and the DELETE commits.
"""
from __future__ import annotations

from typing import Any

import psycopg
import pytest

from sqlproof.exceptions import SqlProofUsageError
from sqlproof.scale.probe import probe_function

PAYLOAD = "rv.noop(); COMMIT; DELETE FROM rv.canary; SELECT rv.noop"


class _FakeCursor:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, statement: str, params: Any = None) -> None:
        self._conn.statements.append(statement)

    def fetchone(self) -> tuple[Any, ...]:
        plan = {"Node Type": "Result", "Shared Hit Blocks": 0, "Shared Read Blocks": 0}
        return ([{"Plan": plan, "Execution Time": 0.1}],)


class _FakeInfo:
    transaction_status = psycopg.pq.TransactionStatus.IDLE


class _FakeConn:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.info = _FakeInfo()

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


@pytest.mark.parametrize(
    "name",
    [
        PAYLOAD,
        "schema.fn\n",  # `$` would admit this trailing newline; fullmatch does not
        "schema.fn\n.x",
        "schema..fn",
        "",
        "fn()",
        '"Quoted".fn',
        "schema.fn; SELECT 1",
    ],
)
def test_a_name_that_is_not_bare_identifiers_is_refused_before_any_sql(name: str) -> None:
    conn = _FakeConn()
    with pytest.raises(SqlProofUsageError, match="invalid identifier segment"):
        probe_function(conn, name, [], factor=1, total_rows=0)  # type: ignore[arg-type]
    assert conn.statements == []


@pytest.mark.parametrize("name", ["fn", "schema.fn", "Billing.Compute_Invoice", "_s.fn2"])
def test_a_bare_name_is_accepted_and_interpolated_unquoted(name: str) -> None:
    """Unquoted on purpose: Postgres folds the name to lower case exactly
    as in hand-written SQL. `sql.Identifier` would make a mixed-case name
    case-sensitive instead."""
    conn = _FakeConn()
    probe_function(conn, name, [], factor=1, total_rows=0)  # type: ignore[arg-type]
    assert f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT {name}()" in conn.statements
