from __future__ import annotations

import re
from typing import Any, Literal, cast

from pglast import parse_sql as parse_postgres_sql
from pglast.enums import ConstrType
from pglast.stream import RawStream

from sqlproof.exceptions import SqlProofSchemaError
from sqlproof.schema.model import (
    CheckConstraint,
    Column,
    ExclusionConstraint,
    ForeignKey,
    PartialUniqueConstraint,
    PgType,
    SchemaInfo,
    Table,
)


def parse_schema_sql(sql: str, *, schema: str = "public") -> SchemaInfo:
    try:
        statements: tuple[Any, ...] = tuple(parse_postgres_sql(sql))
    except Exception as exc:
        raise SqlProofSchemaError(str(exc)) from exc

    enums: list[PgType] = []
    # `type_names` is the lookup table for user-defined types — both
    # enums and domains — so column-type resolution finds them either
    # way.
    type_names: dict[str, PgType] = {}
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
            type_names[enum_name] = enum
            type_names[f"{enum_schema}.{enum_name}"] = enum
        elif type(statement).__name__ == "CreateDomainStmt":
            domain = _parse_domain_statement(statement, schema)
            type_names[domain.name] = domain

    tables = tuple(
        _parse_table_statement(raw_statement.stmt, type_names, schema)
        for raw_statement in statements
        if type(raw_statement.stmt).__name__ == "CreateStmt"
    )
    if not tables and "CREATE TABLE" in sql.upper():
        raise SqlProofSchemaError("Could not parse CREATE TABLE statement.")

    # Pick up partial unique indexes from CREATE [UNIQUE] INDEX
    # statements and graft them onto the corresponding table. Without
    # this the parser silently drops these — schemas relying on the
    # soft-delete pattern (UNIQUE WHERE deleted_at IS NULL) would
    # generate datasets that Postgres rejects at INSERT time.
    tables = _attach_partial_unique_indexes(tables, statements, schema)

    # De-duplicate domains (same domain may appear under qualified and
    # unqualified keys in type_names).
    domains_seq = tuple(t for t in type_names.values() if t.kind == "domain")
    seen_domain_names: set[str] = set()
    unique_domains: list[PgType] = []
    for domain in domains_seq:
        if domain.name not in seen_domain_names:
            unique_domains.append(domain)
            seen_domain_names.add(domain.name)
    return SchemaInfo(
        tables=tables, enums=tuple(enums), domains=tuple(unique_domains)
    )


def _parse_domain_statement(statement: Any, default_schema: str) -> PgType:
    """Parse a CREATE DOMAIN node.

    The base type comes from ``typeName``; CHECK clauses live in
    ``constraints`` as ordinary ``CONSTR_CHECK`` nodes that reference
    a placeholder column named ``VALUE`` (Postgres's convention for
    referring to the domain value inside a CHECK).
    """
    _domain_schema, domain_name = _qualified_parts(
        statement.domainname, default_schema=default_schema
    )
    base = _parse_type_node(statement.typeName, type_names={})
    check_expressions: list[str] = []
    for constraint in statement.constraints or ():
        if constraint.contype == ConstrType.CONSTR_CHECK:
            check_expressions.append(_render(constraint.raw_expr))
    return PgType(
        kind="domain",
        name=domain_name,
        base=base,
        check_expressions=tuple(check_expressions),
    )


def _attach_partial_unique_indexes(
    tables: tuple[Table, ...], statements: tuple[Any, ...], default_schema: str
) -> tuple[Table, ...]:
    by_qname: dict[tuple[str, str], list[PartialUniqueConstraint]] = {}
    for raw_statement in statements:
        statement: Any = raw_statement.stmt
        if type(statement).__name__ != "IndexStmt":
            continue
        if not getattr(statement, "unique", False):
            continue
        if getattr(statement, "whereClause", None) is None:
            # Unconditional CREATE UNIQUE INDEX — outside scope of this
            # change. Inline UNIQUE in CREATE TABLE remains the canonical
            # spelling for unconditional uniques.
            continue
        relation = statement.relation
        table_schema = getattr(relation, "schemaname", None) or default_schema
        table_name = relation.relname
        columns = tuple(_index_param_name(param) for param in statement.indexParams or ())
        predicate = _render(statement.whereClause)
        by_qname.setdefault((table_schema, table_name), []).append(
            PartialUniqueConstraint(columns=columns, predicate=predicate)
        )

    if not by_qname:
        return tables

    return tuple(
        Table(
            schema=t.schema,
            name=t.name,
            columns=t.columns,
            primary_key=t.primary_key,
            foreign_keys=t.foreign_keys,
            unique_constraints=t.unique_constraints,
            check_constraints=t.check_constraints,
            opaque_constraints=t.opaque_constraints,
            partial_unique_constraints=tuple(by_qname.get((t.schema, t.name), ())),
            exclusion_constraints=t.exclusion_constraints,
        )
        for t in tables
    )


def _index_param_name(param: Any) -> str:
    # IndexElem.name is the simple column-name form. Expression-form
    # index params (e.g. CREATE UNIQUE INDEX ... ON t (lower(email)))
    # have name=None and `.expr` set; we don't model those today —
    # render their text so the user sees something stable.
    name = getattr(param, "name", None)
    if name is not None:
        return str(name)
    expr = getattr(param, "expr", None)
    if expr is not None:
        return _render(expr)
    return ""


def _parse_table_statement(statement: Any, type_names: dict[str, PgType], schema: str) -> Table:
    relation = statement.relation
    table_schema = relation.schemaname or schema
    table_name = relation.relname
    columns: list[Column] = []
    primary_key: tuple[str, ...] = ()
    foreign_keys: list[ForeignKey] = []
    unique_constraints: list[tuple[str, ...]] = []
    check_constraints: list[CheckConstraint] = []
    exclusion_constraints: list[ExclusionConstraint] = []

    for element in statement.tableElts or ():
        if type(element).__name__ == "ColumnDef":
            column = _parse_column(element, type_names)
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
        elif element.contype == ConstrType.CONSTR_EXCLUSION:
            exclusion_constraints.append(_parse_exclusion(element))

    return Table(
        schema=table_schema,
        name=table_name,
        columns=tuple(columns),
        primary_key=primary_key,
        foreign_keys=tuple(foreign_keys),
        unique_constraints=tuple(unique_constraints),
        check_constraints=tuple(check_constraints),
        exclusion_constraints=tuple(exclusion_constraints),
    )


def _parse_exclusion(constraint: Any) -> ExclusionConstraint:
    """Parse a table-level EXCLUDE constraint.

    ``constraint.exclusions`` is a tuple of ``(IndexElem, (String,
    ...))`` pairs. The IndexElem gives the column name; the String
    nodes carry the operator name (typically a single operator per
    column). ``constraint.access_method`` is the index AM ("gist",
    "btree", "btree_gist", …) — defaults to "gist" when the user
    omits ``USING`` for an EXCLUDE clause.
    """
    columns_with_operators: list[tuple[str, str]] = []
    for index_elem, operator_strings in constraint.exclusions or ():
        column_name = getattr(index_elem, "name", None)
        if column_name is None:
            # Expression-form (`EXCLUDE (lower(name) WITH =)`) — render
            # the expression text so the model still surfaces something
            # stable. The generator won't enforce it.
            column_name = _render(index_elem.expr) if index_elem.expr is not None else ""
        # Postgres parses each operator into a list of qualified name
        # parts; the actual operator symbol is the last String.
        operator = _sval(operator_strings[-1])
        columns_with_operators.append((column_name, operator))
    access_method = constraint.access_method or "gist"
    return ExclusionConstraint(
        columns_with_operators=tuple(columns_with_operators),
        access_method=access_method,
    )


def _parse_column(column: Any, type_names: dict[str, PgType]) -> Column:
    constraints = tuple(column.constraints or ())
    pg_type = _parse_type_node(column.typeName, type_names)
    identity = _identity_for_constraints(constraints)
    # `GENERATED ALWAYS AS (expr) STORED` columns appear as a
    # CONSTR_GENERATED constraint on the column. Postgres rejects
    # INSERTs that target generated columns, so flag them so the
    # row generator skips them — same as SERIAL/IDENTITY columns.
    has_generated_expr = any(
        constraint.contype == ConstrType.CONSTR_GENERATED for constraint in constraints
    )
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
        is_generated=(
            pg_type.name in {"serial", "bigserial"}
            or identity is not None
            or has_generated_expr
        ),
        identity=identity,
    )


def _parse_type_node(type_node: Any, type_names: dict[str, PgType]) -> PgType:
    parts = tuple(_sval(part) for part in type_node.names)
    name = _normalize_type_name(".".join(parts))
    if name in type_names:
        return type_names[name]
    unqualified = name.rsplit(".", 1)[-1]
    if unqualified in type_names:
        return type_names[unqualified]
    modifiers = tuple(_const_int(modifier) for modifier in type_node.typmods or ())
    range_element = _RANGE_ELEMENT_TYPES.get(unqualified)
    if range_element is not None:
        return PgType(
            kind="range",
            name=unqualified,
            base=PgType(kind="scalar", name=range_element),
            modifiers=modifiers,
        )
    return PgType(kind="scalar", name=unqualified, modifiers=modifiers)


# Builtin Postgres range types mapped to their element type. Custom
# user-defined range types (rare) aren't covered by this static map —
# they'd need pg_range introspection. We keep this in parse_sql because
# both parse_schema_sql and introspect_schema can name-detect off it.
_RANGE_ELEMENT_TYPES: dict[str, str] = {
    "int4range": "integer",
    "int8range": "bigint",
    "numrange": "numeric",
    "tsrange": "timestamp",
    "tstzrange": "timestamptz",
    "daterange": "date",
}


def _parse_foreign_key(constraint: Any, *, columns: tuple[str, ...] | None = None) -> ForeignKey:
    return ForeignKey(
        columns=columns or tuple(_sval(value) for value in constraint.fk_attrs),
        referenced_table=constraint.pktable.relname,
        referenced_columns=tuple(_sval(value) for value in constraint.pk_attrs),
        on_delete=_referential_action(constraint.fk_del_action),
        on_update=_referential_action(constraint.fk_upd_action),
        referenced_schema=getattr(constraint.pktable, "schemaname", None),
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
