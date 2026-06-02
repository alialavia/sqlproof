from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from sqlproof.exceptions import SqlProofSchemaError


@dataclass(frozen=True, slots=True)
class PgType:
    kind: Literal["scalar", "array", "enum", "domain", "composite", "range"]
    name: str
    base: PgType | None = None
    enum_values: tuple[str, ...] = ()
    array_dim: int = 0
    modifiers: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedCheck:
    kind: Literal["range", "in_set", "regex", "length", "compound"]
    column: str
    payload: Any


@dataclass(frozen=True, slots=True)
class CheckConstraint:
    expression: str
    parsed: ParsedCheck | None = None


@dataclass(frozen=True, slots=True)
class PartialUniqueConstraint:
    columns: tuple[str, ...]
    predicate: str


@dataclass(frozen=True, slots=True)
class ExclusionConstraint:
    """Postgres EXCLUDE constraint — one (column, operator) pair
    per element, plus the access method.

    Generalizes UNIQUE: rejects any pair of rows where ALL
    operators evaluate true on the (row_a.col, row_b.col) pairs.
    Canonical example: ``EXCLUDE USING gist (room WITH =, during
    WITH &&)`` prevents two bookings from overlapping in time on
    the same room.
    """

    columns_with_operators: tuple[tuple[str, str], ...]
    access_method: str


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: PgType
    nullable: bool
    default: str | None
    is_generated: bool
    identity: Literal["always", "by_default"] | None = None


@dataclass(frozen=True, slots=True)
class ForeignKey:
    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]
    on_delete: Literal["NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"]
    on_update: Literal["NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"]
    referenced_schema: str | None = None


@dataclass(frozen=True, slots=True)
class Table:
    schema: str
    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKey, ...]
    unique_constraints: tuple[tuple[str, ...], ...]
    check_constraints: tuple[CheckConstraint, ...]
    opaque_constraints: tuple[str, ...] = ()
    partial_unique_constraints: tuple[PartialUniqueConstraint, ...] = ()
    exclusion_constraints: tuple[ExclusionConstraint, ...] = ()

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"

    def column(self, name: str) -> Column:
        for column in self.columns:
            if column.name == name:
                return column
        msg = f"Unknown column {name!r} on table {self.qualified_name!r}."
        raise SqlProofSchemaError(msg)


@dataclass(frozen=True, slots=True)
class Function:
    schema: str
    name: str
    arg_types: tuple[PgType, ...]
    return_type: PgType
    volatility: Literal["immutable", "stable", "volatile"]
    language: str


@dataclass(frozen=True, slots=True)
class SchemaInfo:
    tables: tuple[Table, ...] = ()
    enums: tuple[PgType, ...] = ()
    functions: tuple[Function, ...] = ()
    domains: tuple[PgType, ...] = ()
    opaque_sql: tuple[str, ...] = field(default_factory=tuple)

    def table(self, name: str, schema: str = "public") -> Table:
        for table in self.tables:
            if table.name == name and table.schema == schema:
                return table
        msg = f"Unknown table {schema}.{name}."
        raise SqlProofSchemaError(msg)
