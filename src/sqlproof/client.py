from __future__ import annotations

import re
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import fields, is_dataclass
from typing import Any, Protocol, TypeVar, cast

from sqlproof.exceptions import SqlProofMappingError, SqlProofUsageError

T = TypeVar("T")


class SqlProofClient(Protocol):
    def query(self, sql: str, *params: Any) -> list[dict[str, Any]]: ...

    def query_typed(self, sql: str, model: type[T], *params: Any) -> list[T]: ...

    def scalar(self, sql: str, *params: Any) -> Any: ...

    def execute(self, sql: str, *params: Any) -> int: ...

    def execute_file(self, path: str) -> None: ...

    def savepoint(self) -> AbstractContextManager[None]: ...

    def get_generated_data(self) -> dict[str, list[dict[str, Any]]]: ...


class InMemorySqlProofClient:
    def __init__(self, dataset: dict[str, list[dict[str, Any]]]) -> None:
        self._dataset = dataset

    def query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        del params
        match = re.search(r"SELECT\s+(?P<columns>.*?)\s+FROM\s+(?P<table>\w+)", sql, re.I | re.S)
        if match is None:
            return []
        table = match.group("table")
        rows = self._dataset.get(table, [])
        columns_sql = match.group("columns").strip()
        if columns_sql == "*":
            return [dict(row) for row in rows]
        columns = [_clean_selected_column(part) for part in columns_sql.split(",")]
        return [{column: row.get(column) for column in columns} for row in rows]

    def query_typed(self, sql: str, model: type[T], *params: Any) -> list[T]:
        rows = self.query(sql, *params)
        return [_map_row(row, model) for row in rows]

    def scalar(self, sql: str, *params: Any) -> Any:
        rows = self.query(sql, *params)
        if not rows:
            return None
        return next(iter(rows[0].values()))

    def execute(self, sql: str, *params: Any) -> int:
        del sql, params
        return 0

    def execute_file(self, path: str) -> None:
        del path

    @contextmanager
    def savepoint(self) -> Generator[None]:
        yield

    def get_generated_data(self) -> dict[str, list[dict[str, Any]]]:
        return self._dataset


def _clean_selected_column(sql: str) -> str:
    value = sql.strip()
    if "." in value:
        value = value.rsplit(".", 1)[1]
    if " AS " in value.upper():
        value = re.split(r"\s+AS\s+", value, flags=re.I)[-1]
    return value.strip().strip('"')


def _map_row(row: dict[str, Any], model: type[T]) -> T:
    detectors = [
        hasattr(model, "__pydantic_fields__"),
        is_dataclass(model),
        hasattr(model, "__required_keys__") or hasattr(model, "__optional_keys__"),
    ]
    if sum(detectors) != 1:
        raise SqlProofUsageError("Ambiguous or unsupported row model type.")
    if hasattr(model, "__pydantic_fields__"):
        try:
            return model.model_validate(row)  # type: ignore[attr-defined,no-any-return]
        except Exception as exc:  # pragma: no cover - exercised when pydantic is installed
            raise SqlProofMappingError(str(exc)) from exc
    if is_dataclass(model):
        names = {field.name for field in fields(model)}
        missing = {field.name for field in fields(model) if field.name not in row}
        if missing:
            raise SqlProofMappingError(f"Missing required fields: {', '.join(sorted(missing))}")
        return model(**{name: row[name] for name in names if name in row})
    required = cast("set[str]", getattr(model, "__required_keys__", set[str]()))
    missing = required - row.keys()
    if missing:
        raise SqlProofMappingError(f"Missing required fields: {', '.join(sorted(missing))}")
    return dict(row)  # type: ignore[return-value]
