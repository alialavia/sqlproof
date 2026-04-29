from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlproof import SqlProof
from sqlproof import core as core_module
from sqlproof.client import PsycopgSqlProofClient
from sqlproof.config import SqlProofConfig
from sqlproof.runners import db as db_module
from sqlproof.runners.db import DBManager
from sqlproof.schema.model import SchemaInfo


class FakeCursor:
    def __init__(self, rows: list[dict[str, Any]] | None = None, rowcount: int = 0) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> FakeCursor:
        self.executed.append((sql, params))
        if sql.startswith("SELECT"):
            return FakeCursor([{"id": 1, "name": "Ada"}])
        return FakeCursor(rowcount=3)

    def close(self) -> None:
        self.closed = True


def test_psycopg_client_executes_queries_and_scalars() -> None:
    connection = FakeConnection()
    client = PsycopgSqlProofClient(connection, dataset={"users": [{"id": 1}]})

    assert client.query("SELECT id, name FROM users WHERE id = %s", 1) == [{"id": 1, "name": "Ada"}]
    assert client.scalar("SELECT id FROM users") == 1
    assert client.get_generated_data() == {"users": [{"id": 1}]}
    assert connection.executed[0] == ("SELECT id, name FROM users WHERE id = %s", (1,))


def test_psycopg_client_executes_mutations_files_and_savepoints(tmp_path: Path) -> None:
    connection = FakeConnection()
    client = PsycopgSqlProofClient(connection)
    sql_file = tmp_path / "seed.sql"
    sql_file.write_text(
        "INSERT INTO users VALUES (1);\nINSERT INTO users VALUES (2);",
        encoding="utf-8",
    )

    assert client.execute("UPDATE users SET name = %s", "Ada") == 3
    client.execute_file(sql_file)
    try:
        with client.savepoint():
            client.execute("DELETE FROM users")
            raise RuntimeError("force rollback")
    except RuntimeError:
        pass

    executed_sql = [sql for sql, _params in connection.executed]
    assert "SAVEPOINT sqlproof_client" in executed_sql
    assert "ROLLBACK TO SAVEPOINT sqlproof_client" in executed_sql
    assert "RELEASE SAVEPOINT sqlproof_client" in executed_sql
    assert "INSERT INTO users VALUES (1)" in executed_sql
    assert "INSERT INTO users VALUES (2)" in executed_sql


def test_db_manager_connects_with_connection_string_and_closes(monkeypatch) -> None:
    connection = FakeConnection()
    calls: list[dict[str, Any]] = []

    def fake_connect(**kwargs: Any) -> FakeConnection:
        calls.append(kwargs)
        return connection

    monkeypatch.setattr(db_module.psycopg, "connect", fake_connect)
    manager = DBManager(
        SqlProofConfig(connection_string="postgresql://localhost/postgres", schema="public")
    )

    manager.start()
    with manager.acquire() as client:
        assert isinstance(client, PsycopgSqlProofClient)
        assert client.scalar("SELECT id") == 1
    manager.stop()

    assert calls == [
        {
            "conninfo": "postgresql://localhost/postgres",
            "autocommit": False,
            "row_factory": db_module.dict_row,
        }
    ]
    assert connection.closed is True


def test_sqlproof_from_connection_string_introspects_schema(monkeypatch) -> None:
    connection = FakeConnection()

    def fake_connect(**kwargs: Any) -> FakeConnection:
        assert kwargs["conninfo"] == "postgresql://localhost/postgres"
        return connection

    def fake_introspect(conn: FakeConnection, *, schema: str):
        assert conn is connection
        assert schema == "public"
        return SchemaInfo()

    monkeypatch.setattr(core_module.psycopg, "connect", fake_connect)
    monkeypatch.setattr(core_module, "introspect_schema", fake_introspect)

    proof = SqlProof.from_connection_string("postgresql://localhost/postgres")

    assert proof.schema_info == SchemaInfo()
    assert connection.closed is True
