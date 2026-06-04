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
from sqlproof.generators.rows import table_rows_strategy
from sqlproof.generators.sampling import draw_example
from sqlproof.schema.dependency_graph import resolve_insertion_plan
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

    def row_strategy(
        self,
        table: str,
        /,
        **overrides: object,
    ) -> SearchStrategy[dict[str, Any]]:
        """Schema-backed strategy that draws ONE valid row for ``table``.

        The intended use is ad-hoc test setup — pytest fixtures and
        regression pins that need a row to exist but don't care about
        the bulk-data ergonomics of a full ``check()`` / property test.
        Reach for property tests (``check()``, ``dataset_strategy``)
        first; ``row_strategy`` is the smaller hammer for the residual
        cases where one specific row needs to land in the DB.

        The point of using this over a hand-rolled
        ``INSERT INTO X (a, b, c) VALUES (...)`` helper is that the
        generator knows the schema. When a migration adds a NOT NULL
        column to ``X``, ``row_strategy`` callers automatically receive
        a valid value for it; hand-rolled INSERT strings silently break
        at runtime, often in tests not nominally about ``X``. See
        issue #13 for the failure mode.

        Override keyword arguments may be: a Hypothesis ``SearchStrategy``
        (drawn from), a callable (passed a ``ColumnContext``), or a bare
        value (used as-is). Unknown column names raise immediately so
        typos surface at the call site rather than silently no-op.
        """
        table_obj = self.schema_info.table(table)
        valid_columns = {column.name for column in table_obj.columns}
        unknown = [name for name in overrides if name not in valid_columns]
        if unknown:
            msg = (
                f"row_strategy({table!r}): unknown column(s) in overrides: "
                f"{sorted(unknown)!r}. Valid columns: {sorted(valid_columns)!r}."
            )
            raise SqlProofUsageError(msg)
        namespaced: dict[str, object] = {
            f"{table}.{column}": value for column, value in overrides.items()
        }
        return table_rows_strategy(
            table_obj,
            count=1,
            columns=namespaced,
        ).map(lambda rows: rows[0])

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
        machine_class: type[Any],
        *,
        settings: Any = None,
    ) -> None:
        """Run a `SqlProofStateMachine` subclass against this proof.

        Binds `self` as the proof for the machine, then dispatches to
        `hypothesis.stateful.run_state_machine_as_test`. Each example gets
        an isolated dataset client; writes from one example are rolled back
        before the next begins.
        """
        from hypothesis.stateful import (
            run_state_machine_as_test,  # pyright: ignore[reportUnknownVariableType]
        )

        from sqlproof.testing import SqlProofStateMachine

        if not issubclass(machine_class, SqlProofStateMachine):
            msg = "machine_class must be a subclass of SqlProofStateMachine."
            raise SqlProofUsageError(msg)

        bound_class = type(
            machine_class.__name__,
            (machine_class,),
            {"_sqlproof_proof": self},
        )
        runner = cast("Callable[..., None]", run_state_machine_as_test)
        runner(bound_class, settings=settings)

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
    """Insert the generated dataset into the database in FK-safe order.

    Two passes when the schema has cycles resolvable via deferred FKs
    (see ``sqlproof.schema.dependency_graph`` for the algorithm):

      1. INSERT pass — every row, in topological order of the FK
         graph WITH deferred edges removed. Deferred FK columns are
         excluded from the column list and INSERTed as NULL (the
         column's default, since deferral requires nullable columns).

      2. UPDATE pass — for each deferred edge, populate the deferred
         FK columns by sampling a referenced row. The dataset
         generator left these columns NULL on purpose; this pass
         picks an arbitrary referenced row per source row to satisfy
         the FK after both rows exist.

    Schemas without cycles produce zero deferred edges; the UPDATE
    pass is a no-op and behavior is identical to the pre-cycle-
    handling code path. See #47 for the original repro.
    """
    if not any(rows for rows in dataset.values()):
        return

    plan = resolve_insertion_plan(schema_info.tables)

    # Map source_table -> set of deferred FK column names. Used to
    # filter columns out of the INSERT statement.
    deferred_columns_by_table: dict[str, set[str]] = {}
    for edge in plan.deferred_edges:
        deferred_columns_by_table.setdefault(edge.source_table, set()).update(edge.fk_columns)

    # --- Pass 1: INSERT all rows in dependency order, skipping deferred FK columns.
    for table in plan.ordered_tables:
        rows = dataset.get(table.name, [])
        deferred_cols = deferred_columns_by_table.get(table.name, set())
        for row in rows:
            if not row:
                continue
            columns = [c for c in row if c not in deferred_cols]
            if not columns:
                # All columns deferred — let the DB's defaults handle it.
                # Rare/contrived case; safe to skip.
                continue
            placeholders = ", ".join(["%s"] * len(columns))
            column_sql = ", ".join(_quote_identifier(column) for column in columns)
            table_sql = f"{_quote_identifier(table.schema)}.{_quote_identifier(table.name)}"
            sql = f"INSERT INTO {table_sql} ({column_sql}) VALUES ({placeholders})"
            values = [_adapt_insert_value(table, column, row[column]) for column in columns]
            client.execute(sql, *values)

    # --- Pass 2: UPDATE deferred FK columns now that both ends exist.
    if not plan.deferred_edges:
        return

    by_name = {t.name: t for t in schema_info.tables}
    for edge in plan.deferred_edges:
        source_rows = dataset.get(edge.source_table, [])
        ref_rows = dataset.get(edge.referenced_table, [])
        if not source_rows or not ref_rows:
            # No source rows or no targets to point at — UPDATE would
            # find/touch nothing, so skip.
            continue
        source_table = by_name[edge.source_table]
        ref_table = by_name[edge.referenced_table]
        table_sql = (
            f"{_quote_identifier(source_table.schema)}."
            f"{_quote_identifier(source_table.name)}"
        )
        set_clause = ", ".join(
            f"{_quote_identifier(col)} = %s" for col in edge.fk_columns
        )
        # WHERE clause matches the source row by its primary key. We
        # require the dataset to have included the PK columns in each
        # row (the generator always does — see rows.py's PK handling).
        if not source_table.primary_key:
            continue  # Pathological schema; can't UPDATE without an identity.
        where_clause = " AND ".join(
            f"{_quote_identifier(pk)} = %s" for pk in source_table.primary_key
        )
        sql = f"UPDATE {table_sql} SET {set_clause} WHERE {where_clause}"

        # Pick a referenced row per source row. Deterministic-ish
        # (round-robin via modulo) so the same dataset produces the
        # same UPDATEs across reruns — helpful when shrinking.
        for i, source_row in enumerate(source_rows):
            if not source_row:
                continue
            ref_row = ref_rows[i % len(ref_rows)]
            ref_values = [
                _adapt_insert_value(ref_table, ref_col, ref_row[ref_col])
                for ref_col in edge.referenced_columns
            ]
            pk_values = [
                _adapt_insert_value(source_table, pk, source_row[pk])
                for pk in source_table.primary_key
            ]
            client.execute(sql, *ref_values, *pk_values)


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
