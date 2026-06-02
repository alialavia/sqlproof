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
    # Raw CHECK expressions inherited from a `CREATE DOMAIN` clause
    # (e.g. `("VALUE > 0",)`). Empty for non-domain types or for
    # domains without CHECK constraints. The row generator substitutes
    # `VALUE` for the actual column name and feeds the result through
    # the standard CHECK-refinement pipeline.
    check_expressions: tuple[str, ...] = ()


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
