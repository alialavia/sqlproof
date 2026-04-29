from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

import psycopg
from psycopg.rows import dict_row

from sqlproof.client import InMemorySqlProofClient, SqlProofClient
from sqlproof.config import SqlProofConfig
from sqlproof.exceptions import SqlProofPropertyFailure
from sqlproof.generators.graph import dataset_strategy
from sqlproof.generators.sampling import draw_example
from sqlproof.schema.dependency_graph import insertion_order
from sqlproof.schema.fingerprint import compute
from sqlproof.schema.introspect import introspect_schema
from sqlproof.schema.model import SchemaInfo
from sqlproof.schema.parse_sql import parse_schema_sql


class SqlProof:
    def __init__(self, config: SqlProofConfig) -> None:
        from sqlproof.runners.db import DBManager

        self.config = config
        self.schema_info = self._load_schema(config)
        self.schema_fingerprint = compute(self.schema_info)
        self._db_manager = DBManager(config) if config.connection_string is not None else None

    @classmethod
    def from_schema_file(cls, path: str | Path, **kwargs: Any) -> Self:
        return cls(SqlProofConfig(schema_file=path, **kwargs))

    @classmethod
    def from_connection_string(cls, dsn: str, **kwargs: Any) -> Self:
        return cls(SqlProofConfig(connection_string=dsn, **kwargs))

    @classmethod
    def from_config(cls, config: SqlProofConfig) -> Self:
        return cls(config)

    def customize(self, table: str, **overrides: object) -> Self:
        del table, overrides
        return self

    @contextmanager
    def client_for_dataset(
        self, dataset: dict[str, list[dict[str, Any]]]
    ) -> Generator[SqlProofClient]:
        if self._db_manager is None:
            yield InMemorySqlProofClient(dataset)
            return
        with self._db_manager.acquire() as client:
            client.execute("SAVEPOINT sqlproof_run")
            try:
                _insert_dataset(client, self.schema_info, dataset)
                yield client
            finally:
                client.execute("ROLLBACK TO SAVEPOINT sqlproof_run")
                client.execute("RELEASE SAVEPOINT sqlproof_run")

    def check(
        self,
        name: str,
        *,
        sizes: dict[str, int],
        property: Callable[..., None],
        setup: object | None = None,
        runs: int = 100,
        seed: int | None = None,
        timeout_ms: int = 5000,
        commit: bool = False,
    ) -> None:
        from sqlproof.runners.property import run_property

        del name, setup, seed, timeout_ms, commit
        if not callable(property):
            msg = "property must be callable"
            raise TypeError(msg)
        run_property(self, property, sizes=sizes, runs=runs, failure_dir=Path(".sqlproof/failures"))

    def invariant(
        self,
        name: str,
        *,
        sizes: dict[str, int],
        query: str,
        expect_empty: bool = True,
        runs: int = 100,
        seed: int | None = None,
        timeout_ms: int = 5000,
    ) -> None:
        del seed, timeout_ms
        strategy = dataset_strategy(self.schema_info, sizes=sizes)
        for run_index in range(runs):
            client = InMemorySqlProofClient(draw_example(strategy))
            rows = client.query(query)
            failed = bool(rows) if expect_empty else not rows
            if failed:
                payload = {
                    "property_name": name,
                    "runs": run_index + 1,
                    "row_context": {},
                    "dataset": client.get_generated_data(),
                    "schema_fingerprint": self.schema_fingerprint,
                }
                raise SqlProofPropertyFailure(
                    f"Invariant {name!r} failed: query returned {len(rows)} rows.",
                    counterexample=payload,
                )

    def disconnect(self) -> None:
        if self._db_manager is not None:
            self._db_manager.stop()
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.disconnect()

    @staticmethod
    def _load_schema(config: SqlProofConfig) -> SchemaInfo:
        if config.schema_file is not None:
            path = Path(config.schema_file)
            return parse_schema_sql(path.read_text(encoding="utf-8"), schema=config.schema)
        if config.connection_string is not None:
            connection = psycopg.connect(
                conninfo=config.connection_string,
                autocommit=True,
                row_factory=cast(Any, dict_row),
            )
            try:
                return introspect_schema(connection, schema=config.schema)
            finally:
                connection.close()
        return SchemaInfo()


def _insert_dataset(
    client: SqlProofClient,
    schema_info: SchemaInfo,
    dataset: dict[str, list[dict[str, Any]]],
) -> None:
    for table in insertion_order(schema_info.tables):
        for row in dataset.get(table.name, []):
            if not row:
                continue
            columns = list(row)
            placeholders = ", ".join(["%s"] * len(columns))
            column_sql = ", ".join(_quote_identifier(column) for column in columns)
            table_sql = f"{_quote_identifier(table.schema)}.{_quote_identifier(table.name)}"
            sql = f"INSERT INTO {table_sql} ({column_sql}) VALUES ({placeholders})"
            client.execute(sql, *(row[column] for column in columns))


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'
