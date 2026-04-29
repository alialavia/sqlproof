from __future__ import annotations

import re
from typing import Any, Literal, cast

from pglast import parse_sql as parse_postgres_sql
from pglast.enums import ConstrType
from pglast.stream import RawStream

from sqlproof.exceptions import SqlProofSchemaError
from sqlproof.schema.model import CheckConstraint, Column, ForeignKey, PgType, SchemaInfo, Table


def parse_schema_sql(sql: str, *, schema: str = "public") -> SchemaInfo:
    try:
        statements: tuple[Any, ...] = tuple(parse_postgres_sql(sql))
    except Exception as exc:
        raise SqlProofSchemaError(str(exc)) from exc

    enums: list[PgType] = []
    enum_names: dict[str, PgType] = {}
    for raw_statement in statements:
        statement: Any = raw_statement.stmt
        if type(statement).__name__ == "CreateEnumStmt":
            enum_schema, enum_name = _qualified_parts(statement.typeName, default_schema=schema)
            enum = PgType(
                kind="enum",
                name=enum_name,
                enum_values=tuple(_sval(value) for value in statement.vals),
            )
            enums.append(enum)
            enum_names[enum_name] = enum
            enum_names[f"{enum_schema}.{enum_name}"] = enum

    tables = tuple(
        _parse_table_statement(raw_statement.stmt, enum_names, schema)
        for raw_statement in statements
        if type(raw_statement.stmt).__name__ == "CreateStmt"
    )
    if not tables and "CREATE TABLE" in sql.upper():
        raise SqlProofSchemaError("Could not parse CREATE TABLE statement.")
    return SchemaInfo(tables=tables, enums=tuple(enums))


def _parse_table_statement(statement: Any, enum_names: dict[str, PgType], schema: str) -> Table:
    relation = statement.relation
    table_schema = relation.schemaname or schema
    table_name = relation.relname
    columns: list[Column] = []
    primary_key: tuple[str, ...] = ()
    foreign_keys: list[ForeignKey] = []
    unique_constraints: list[tuple[str, ...]] = []
    check_constraints: list[CheckConstraint] = []

    for element in statement.tableElts or ():
        if type(element).__name__ == "ColumnDef":
            column = _parse_column(element, enum_names)
            columns.append(column)
            for constraint in element.constraints or ():
                if constraint.contype == ConstrType.CONSTR_PRIMARY:
                    primary_key = (column.name,)
                elif constraint.contype == ConstrType.CONSTR_UNIQUE:
                    unique_constraints.append((column.name,))
                elif constraint.contype == ConstrType.CONSTR_FOREIGN:
                    foreign_keys.append(_parse_foreign_key(constraint, columns=(column.name,)))
                elif constraint.contype == ConstrType.CONSTR_CHECK:
                    check_constraints.append(_parse_check(constraint))
            continue
        if element.contype == ConstrType.CONSTR_PRIMARY:
            primary_key = _constraint_keys(element)
        elif element.contype == ConstrType.CONSTR_UNIQUE:
            unique_constraints.append(_constraint_keys(element))
        elif element.contype == ConstrType.CONSTR_FOREIGN:
            foreign_keys.append(_parse_foreign_key(element))
        elif element.contype == ConstrType.CONSTR_CHECK:
            check_constraints.append(_parse_check(element))

    return Table(
        schema=table_schema,
        name=table_name,
        columns=tuple(columns),
        primary_key=primary_key,
        foreign_keys=tuple(foreign_keys),
        unique_constraints=tuple(unique_constraints),
        check_constraints=tuple(check_constraints),
    )


def _parse_column(column: Any, enum_names: dict[str, PgType]) -> Column:
    constraints = tuple(column.constraints or ())
    pg_type = _parse_type_node(column.typeName, enum_names)
    identity = _identity_for_constraints(constraints)
    primary = any(constraint.contype == ConstrType.CONSTR_PRIMARY for constraint in constraints)
    not_null = (
        column.is_not_null
        or primary
        or any(constraint.contype == ConstrType.CONSTR_NOTNULL for constraint in constraints)
    )
    return Column(
        name=column.colname,
        type=pg_type,
        nullable=not not_null,
        default=_default_for_constraints(constraints),
        is_generated=pg_type.name in {"serial", "bigserial"} or identity is not None,
        identity=identity,
    )


def _parse_type_node(type_node: Any, enum_names: dict[str, PgType]) -> PgType:
    parts = tuple(_sval(part) for part in type_node.names)
    name = _normalize_type_name(".".join(parts))
    if name in enum_names:
        return enum_names[name]
    unqualified = name.rsplit(".", 1)[-1]
    if unqualified in enum_names:
        return enum_names[unqualified]
    modifiers = tuple(_const_int(modifier) for modifier in type_node.typmods or ())
    return PgType(kind="scalar", name=unqualified, modifiers=modifiers)


def _parse_foreign_key(constraint: Any, *, columns: tuple[str, ...] | None = None) -> ForeignKey:
    return ForeignKey(
        columns=columns or tuple(_sval(value) for value in constraint.fk_attrs),
        referenced_table=constraint.pktable.relname,
        referenced_columns=tuple(_sval(value) for value in constraint.pk_attrs),
        on_delete=_referential_action(constraint.fk_del_action),
        on_update=_referential_action(constraint.fk_upd_action),
    )


def _parse_check(constraint: Any) -> CheckConstraint:
    return CheckConstraint(_render(constraint.raw_expr))


def _constraint_keys(constraint: Any) -> tuple[str, ...]:
    return tuple(_sval(value) for value in constraint.keys)


def _default_for_constraints(constraints: tuple[Any, ...]) -> str | None:
    for constraint in constraints:
        if constraint.contype == ConstrType.CONSTR_DEFAULT:
            return _render(constraint.raw_expr)
    return None


def _identity_for_constraints(
    constraints: tuple[Any, ...],
) -> Literal["always", "by_default"] | None:
    for constraint in constraints:
        if constraint.contype == ConstrType.CONSTR_IDENTITY:
            if constraint.generated_when == "a":
                return "always"
            if constraint.generated_when == "d":
                return "by_default"
    return None


def _referential_action(
    action: str,
) -> Literal["NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"]:
    return cast(
        Literal["NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"],
        {
            "a": "NO ACTION",
            "r": "RESTRICT",
            "c": "CASCADE",
            "n": "SET NULL",
            "d": "SET DEFAULT",
            "\x00": "NO ACTION",
        }.get(action, "NO ACTION"),
    )


def _qualified_parts(parts: tuple[Any, ...], *, default_schema: str) -> tuple[str, str]:
    values = tuple(_sval(part) for part in parts)
    if len(values) == 1:
        return default_schema, values[0]
    return values[-2], values[-1]


def _sval(node: Any) -> str:
    return str(node.sval)


def _const_int(node: Any) -> int:
    return int(node.val.ival)


def _render(node: Any) -> str:
    stream = RawStream()  # type: ignore[no-untyped-call]
    return str(stream(node))


def _normalize_type_name(name: str) -> str:
    normalized = re.sub(r"\s+", " ", name.lower())
    return {
        "pg_catalog.int2": "smallint",
        "pg_catalog.int4": "integer",
        "pg_catalog.int8": "bigint",
        "pg_catalog.float4": "real",
        "pg_catalog.float8": "double precision",
        "pg_catalog.bool": "boolean",
        "pg_catalog.varchar": "varchar",
        "pg_catalog.bpchar": "char",
    }.get(normalized, normalized)
