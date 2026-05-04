from __future__ import annotations

from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self, cast

import psycopg
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy
from psycopg.rows import dict_row
from psycopg.types.json import Json, Jsonb

from sqlproof.client import InMemorySqlProofClient, PsycopgSqlProofClient, SqlProofClient
from sqlproof.config import ExternalSeed, ExternalTableSpec, SqlProofConfig
from sqlproof.exceptions import SqlProofPropertyFailure, SqlProofUsageError
from sqlproof.generators.graph import ColumnOverrides, Dataset, SizeSpec, dataset_strategy
from sqlproof.generators.sampling import draw_example
from sqlproof.schema.dependency_graph import insertion_order
from sqlproof.schema.fingerprint import compute
from sqlproof.schema.introspect import introspect_schema
from sqlproof.schema.model import Column, SchemaInfo, Table
from sqlproof.schema.parse_sql import parse_schema_sql


class SqlProof:
    def __init__(self, config: SqlProofConfig) -> None:
        from sqlproof.runners.db import DBManager

        self.config = config
        self.schema_info = self._load_schema(config)
        self.schema_fingerprint = compute(self.schema_info)
        self._db_manager = DBManager(config) if config.connection_string is not None else None
        self._external_sample_cache: dict[str, list[object]] = {}

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

    def dataset_strategy(
        self,
        *,
        sizes: Mapping[str, SizeSpec],
        columns: ColumnOverrides | None = None,
    ) -> SearchStrategy[Dataset]:
        if self.config.external_tables:
            return self._dataset_strategy_with_external_tables(sizes=sizes, columns=columns)
        return dataset_strategy(
            self.schema_info,
            sizes=sizes,
            columns=columns,
        )

    def _dataset_strategy_with_external_tables(
        self,
        *,
        sizes: Mapping[str, SizeSpec],
        columns: ColumnOverrides | None,
    ) -> SearchStrategy[Dataset]:
        @st.composite
        def dataset(draw: st.DrawFn) -> Dataset:
            external_parent_rows = self._external_parent_rows(draw=draw)
            return draw(
                dataset_strategy(
                    self.schema_info,
                    sizes=sizes,
                    external_parent_rows=external_parent_rows,
                    columns=columns,
                )
            )

        return dataset()

    def run_state_machine(
        self,
        machine_class: type,
        *,
        settings: Any = None,
    ) -> None:
        """Run a `SqlProofStateMachine` subclass against this proof.

        Binds `self` as the proof for the machine, then dispatches to
        `hypothesis.stateful.run_state_machine_as_test`. Each example gets
        an isolated dataset client; writes from one example are rolled back
        before the next begins.
        """
        from hypothesis.stateful import run_state_machine_as_test

        from sqlproof.testing import SqlProofStateMachine

        if not isinstance(machine_class, type) or not issubclass(
            machine_class, SqlProofStateMachine
        ):
            msg = "machine_class must be a subclass of SqlProofStateMachine."
            raise SqlProofUsageError(msg)

        bound_class = type(
            machine_class.__name__,
            (machine_class,),
            {"_sqlproof_proof": self},
        )
        run_state_machine_as_test(bound_class, settings=settings)

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
        sizes: Mapping[str, SizeSpec],
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
        sizes: Mapping[str, SizeSpec],
        query: str,
        expect_empty: bool = True,
        runs: int = 100,
        seed: int | None = None,
        timeout_ms: int = 5000,
    ) -> None:
        del seed, timeout_ms
        strategy = self.dataset_strategy(sizes=sizes)
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

    def _external_parent_rows(
        self,
        *,
        draw: st.DrawFn | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        if not self.config.external_tables:
            return {}
        if self.config.connection_string is None:
            msg = "external_tables requires a connection_string-backed SqlProof instance."
            raise SqlProofUsageError(msg)

        connection = psycopg.connect(
            conninfo=self.config.connection_string,
            autocommit=True,
            row_factory=cast(Any, dict_row),
        )
        try:
            client = PsycopgSqlProofClient(connection)
            return _external_parent_rows(
                self.config.external_tables,
                client,
                draw=draw,
                sample_cache=self._external_sample_cache,
            )
        finally:
            connection.close()


def _insert_dataset(
    client: SqlProofClient,
    schema_info: SchemaInfo,
    dataset: dict[str, list[dict[str, Any]]],
) -> None:
    for table in insertion_order(schema_info.tables):
        rows = dataset.get(table.name, [])
        for row in rows:
            if not row:
                continue
            columns = list(row)
            placeholders = ", ".join(["%s"] * len(columns))
            column_sql = ", ".join(_quote_identifier(column) for column in columns)
            table_sql = f"{_quote_identifier(table.schema)}.{_quote_identifier(table.name)}"
            sql = f"INSERT INTO {table_sql} ({column_sql}) VALUES ({placeholders})"
            values = [_adapt_insert_value(table, column, row[column]) for column in columns]
            client.execute(sql, *values)


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _adapt_insert_value(table: Table, column_name: str, value: Any) -> object:
    column = table.column(column_name)
    type_name = _base_type_name(column)
    if type_name == "jsonb":
        return Jsonb(value)
    if type_name == "json":
        return Json(value)
    return value


def _base_type_name(column: Column) -> str:
    pg_type = column.type
    while pg_type.base is not None:
        pg_type = pg_type.base
    return pg_type.name.lower()


def _external_parent_rows(
    specs: Mapping[str, ExternalTableSpec],
    client: SqlProofClient,
    *,
    draw: st.DrawFn | None = None,
    sample_cache: dict[str, list[object]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    rows_by_table: dict[str, list[dict[str, Any]]] = {}
    for table_name, spec in specs.items():
        seed_count = _draw_seed_count(spec.seed_count, draw=draw)
        if spec.seed is not None:
            _call_external_seed(spec.seed, client, seed_count)
        sampled_values = _sample_external_values(
            table_name,
            spec,
            client,
            sample_cache=sample_cache,
        )
        if seed_count is not None:
            sampled_values = sampled_values[:seed_count]
        rows = [{spec.primary_key: value} for value in sampled_values]
        rows_by_table[table_name] = rows
        if "." in table_name:
            rows_by_table.setdefault(table_name.rsplit(".", 1)[1], rows)
    return rows_by_table


def _sample_external_values(
    table_name: str,
    spec: ExternalTableSpec,
    client: SqlProofClient,
    *,
    sample_cache: dict[str, list[object]] | None,
) -> list[object]:
    if spec.seed is not None or sample_cache is None:
        return list(spec.sample(client))
    if table_name not in sample_cache:
        sample_cache[table_name] = list(spec.sample(client))
    return sample_cache[table_name]


def _draw_seed_count(size: SizeSpec | None, *, draw: st.DrawFn | None) -> int | None:
    if size is None:
        return None
    if isinstance(size, int):
        return size
    if draw is None:
        msg = "ExternalTableSpec.seed_count strategies require dataset_strategy() generation."
        raise SqlProofUsageError(msg)
    return draw(size)


def _call_external_seed(
    seed: ExternalSeed,
    client: SqlProofClient,
    count: int | None,
) -> None:
    if count is None:
        cast(Callable[[SqlProofClient], None], seed)(client)
        return
    cast(Callable[[SqlProofClient, int], None], seed)(client, count)
