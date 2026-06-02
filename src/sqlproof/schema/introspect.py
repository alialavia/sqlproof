from __future__ import annotations

from typing import Any, Literal, cast

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


def introspect_schema(connection: Any, *, schema: str = "public") -> SchemaInfo:
    enums = _load_enums(connection, schema=schema)
    enum_by_name = {enum.name: enum for enum in enums}
    columns_by_table: dict[tuple[str, str], list[Column]] = {}
    for row in _fetch_all(connection, _COLUMNS_SQL, schema):
        key = (str(row["schema_name"]), str(row["table_name"]))
        type_name = str(row["type_name"])
        pg_type = enum_by_name.get(type_name, PgType(kind="scalar", name=type_name))
        columns_by_table.setdefault(key, []).append(
            Column(
                name=str(row["column_name"]),
                type=pg_type,
                nullable=bool(row["nullable"]),
                default=_optional_str(row["default"]),
                is_generated=bool(row["is_generated"]) or _is_serial_default(row["default"]),
                identity=_identity(row["identity"]),
            )
        )

    primary_keys = _constraint_columns(connection, _PRIMARY_KEYS_SQL, schema)
    unique_constraints = _grouped_constraints(connection, _UNIQUES_SQL, schema)
    foreign_keys = _foreign_keys(connection, schema=schema)
    check_constraints = _check_constraints(connection, schema=schema)
    partial_unique_constraints = _partial_unique_constraints(connection, schema=schema)
    exclusion_constraints = _exclusion_constraints(connection, schema=schema)

    tables = [
        Table(
            schema=table_schema,
            name=table_name,
            columns=tuple(columns),
            primary_key=primary_keys.get((table_schema, table_name), ()),
            foreign_keys=tuple(foreign_keys.get((table_schema, table_name), ())),
            unique_constraints=tuple(unique_constraints.get((table_schema, table_name), ())),
            check_constraints=tuple(check_constraints.get((table_schema, table_name), ())),
            partial_unique_constraints=tuple(
                partial_unique_constraints.get((table_schema, table_name), ())
            ),
            exclusion_constraints=tuple(
                exclusion_constraints.get((table_schema, table_name), ())
            ),
        )
        for (table_schema, table_name), columns in sorted(columns_by_table.items())
    ]
    return SchemaInfo(tables=tuple(tables), enums=enums)


def _fetch_all(connection: Any, sql: str, *params: object) -> list[dict[str, Any]]:
    cursor = connection.execute(sql, params)
    return [dict(row) for row in cursor.fetchall()]


def _load_enums(connection: Any, *, schema: str) -> tuple[PgType, ...]:
    rows = _fetch_all(connection, _ENUMS_SQL, schema)
    return tuple(
        PgType(
            kind="enum",
            name=str(row["enum_name"]),
            enum_values=tuple(str(value) for value in row["enum_values"]),
        )
        for row in rows
    )


def _constraint_columns(
    connection: Any, sql: str, schema: str
) -> dict[tuple[str, str], tuple[str, ...]]:
    return {
        (str(row["schema_name"]), str(row["table_name"])): tuple(
            str(value) for value in row["columns"]
        )
        for row in _fetch_all(connection, sql, schema)
    }


def _grouped_constraints(
    connection: Any, sql: str, schema: str
) -> dict[tuple[str, str], list[tuple[str, ...]]]:
    grouped: dict[tuple[str, str], list[tuple[str, ...]]] = {}
    for row in _fetch_all(connection, sql, schema):
        key = (str(row["schema_name"]), str(row["table_name"]))
        grouped.setdefault(key, []).append(tuple(str(value) for value in row["columns"]))
    return grouped


def _foreign_keys(connection: Any, *, schema: str) -> dict[tuple[str, str], list[ForeignKey]]:
    grouped: dict[tuple[str, str], list[ForeignKey]] = {}
    for row in _fetch_all(connection, _FOREIGN_KEYS_SQL, schema):
        key = (str(row["schema_name"]), str(row["table_name"]))
        grouped.setdefault(key, []).append(
            ForeignKey(
                columns=tuple(str(value) for value in row["columns"]),
                referenced_table=str(row["referenced_table"]),
                referenced_columns=tuple(str(value) for value in row["referenced_columns"]),
                on_delete=cast(
                    Literal["NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"],
                    str(row["on_delete"]),
                ),
                on_update=cast(
                    Literal["NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"],
                    str(row["on_update"]),
                ),
                referenced_schema=str(row["referenced_schema"]),
            )
        )
    return grouped


def _check_constraints(
    connection: Any, *, schema: str
) -> dict[tuple[str, str], list[CheckConstraint]]:
    grouped: dict[tuple[str, str], list[CheckConstraint]] = {}
    for row in _fetch_all(connection, _CHECKS_SQL, schema):
        key = (str(row["schema_name"]), str(row["table_name"]))
        grouped.setdefault(key, []).append(CheckConstraint(str(row["expression"])))
    return grouped


def _partial_unique_constraints(
    connection: Any, *, schema: str
) -> dict[tuple[str, str], list[PartialUniqueConstraint]]:
    grouped: dict[tuple[str, str], list[PartialUniqueConstraint]] = {}
    for row in _fetch_all(connection, _PARTIAL_UNIQUE_SQL, schema):
        key = (str(row["schema_name"]), str(row["table_name"]))
        grouped.setdefault(key, []).append(
            PartialUniqueConstraint(
                columns=tuple(str(value) for value in row["columns"]),
                predicate=str(row["predicate"]),
            )
        )
    return grouped


def _exclusion_constraints(
    connection: Any, *, schema: str
) -> dict[tuple[str, str], list[ExclusionConstraint]]:
    grouped: dict[tuple[str, str], list[ExclusionConstraint]] = {}
    for row in _fetch_all(connection, _EXCLUSIONS_SQL, schema):
        key = (str(row["schema_name"]), str(row["table_name"]))
        columns = [str(c) for c in row["columns"]]
        operators = [str(o) for o in row["operators"]]
        grouped.setdefault(key, []).append(
            ExclusionConstraint(
                columns_with_operators=tuple(zip(columns, operators, strict=True)),
                access_method=str(row["access_method"]),
            )
        )
    return grouped


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _identity(value: object) -> Literal["always", "by_default"] | None:
    if value is None:
        return None
    normalized = str(value).lower().replace(" ", "_")
    if normalized in {"always", "by_default"}:
        return cast(Literal["always", "by_default"], normalized)
    return None


def _is_serial_default(value: object) -> bool:
    return isinstance(value, str) and value.startswith("nextval(")


_ENUMS_SQL = """
SELECT
  n.nspname AS schema_name,
  t.typname AS enum_name,
  array_agg(e.enumlabel ORDER BY e.enumsortorder) AS enum_values
FROM pg_catalog.pg_type t
JOIN pg_catalog.pg_enum e ON e.enumtypid = t.oid
JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = %s
GROUP BY n.nspname, t.typname
ORDER BY n.nspname, t.typname
"""

_COLUMNS_SQL = """
SELECT
  ns.nspname AS schema_name,
  cls.relname AS table_name,
  att.attname AS column_name,
  typ.typname AS type_name,
  NOT att.attnotnull AS nullable,
  pg_get_expr(def.adbin, def.adrelid) AS default,
  att.attgenerated <> '' AS is_generated,
  CASE att.attidentity WHEN 'a' THEN 'always' WHEN 'd' THEN 'by default' ELSE NULL END AS identity,
  ARRAY[]::integer[] AS modifiers
FROM pg_catalog.pg_attribute att
JOIN pg_catalog.pg_class cls ON cls.oid = att.attrelid
JOIN pg_catalog.pg_namespace ns ON ns.oid = cls.relnamespace
JOIN pg_catalog.pg_type typ ON typ.oid = att.atttypid
LEFT JOIN pg_catalog.pg_attrdef def ON def.adrelid = att.attrelid AND def.adnum = att.attnum
WHERE ns.nspname = %s
  AND cls.relkind IN ('r', 'p')
  AND att.attnum > 0
  AND NOT att.attisdropped
ORDER BY ns.nspname, cls.relname, att.attnum
"""

_PRIMARY_KEYS_SQL = """
SELECT
  ns.nspname AS schema_name,
  cls.relname AS table_name,
  array_agg(att.attname ORDER BY key_column.ordinality) AS columns
FROM pg_catalog.pg_constraint con
JOIN pg_catalog.pg_class cls ON cls.oid = con.conrelid
JOIN pg_catalog.pg_namespace ns ON ns.oid = cls.relnamespace
JOIN unnest(con.conkey) WITH ORDINALITY AS key_column(attnum, ordinality) ON true
JOIN pg_catalog.pg_attribute att ON att.attrelid = cls.oid AND att.attnum = key_column.attnum
WHERE ns.nspname = %s AND con.contype = 'p'
GROUP BY ns.nspname, cls.relname, con.oid
"""

_UNIQUES_SQL = _PRIMARY_KEYS_SQL.replace("con.contype = 'p'", "con.contype = 'u'")

_FOREIGN_KEYS_SQL = """
SELECT
  ns.nspname AS schema_name,
  cls.relname AS table_name,
  array_agg(src_att.attname ORDER BY src_key.ordinality) AS columns,
  ref_ns.nspname AS referenced_schema,
  ref_cls.relname AS referenced_table,
  array_agg(ref_att.attname ORDER BY ref_key.ordinality) AS referenced_columns,
  CASE con.confdeltype
    WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL'
    WHEN 'd' THEN 'SET DEFAULT' ELSE 'NO ACTION'
  END AS on_delete,
  CASE con.confupdtype
    WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE' WHEN 'n' THEN 'SET NULL'
    WHEN 'd' THEN 'SET DEFAULT' ELSE 'NO ACTION'
  END AS on_update
FROM pg_catalog.pg_constraint con
JOIN pg_catalog.pg_class cls ON cls.oid = con.conrelid
JOIN pg_catalog.pg_namespace ns ON ns.oid = cls.relnamespace
JOIN pg_catalog.pg_class ref_cls ON ref_cls.oid = con.confrelid
JOIN pg_catalog.pg_namespace ref_ns ON ref_ns.oid = ref_cls.relnamespace
JOIN unnest(con.conkey) WITH ORDINALITY AS src_key(attnum, ordinality) ON true
JOIN unnest(con.confkey) WITH ORDINALITY AS ref_key(attnum, ordinality)
  ON ref_key.ordinality = src_key.ordinality
JOIN pg_catalog.pg_attribute src_att
  ON src_att.attrelid = cls.oid AND src_att.attnum = src_key.attnum
JOIN pg_catalog.pg_attribute ref_att
  ON ref_att.attrelid = ref_cls.oid AND ref_att.attnum = ref_key.attnum
WHERE ns.nspname = %s AND con.contype = 'f'
GROUP BY ns.nspname, cls.relname, ref_ns.nspname, ref_cls.relname, con.oid
"""

_CHECKS_SQL = """
SELECT
  ns.nspname AS schema_name,
  cls.relname AS table_name,
  pg_get_constraintdef(con.oid, true) AS expression
FROM pg_catalog.pg_constraint con
JOIN pg_catalog.pg_class cls ON cls.oid = con.conrelid
JOIN pg_catalog.pg_namespace ns ON ns.oid = cls.relnamespace
WHERE ns.nspname = %s AND con.contype = 'c'
ORDER BY ns.nspname, cls.relname, con.conname
"""

# Partial unique indexes — uniqueness conditioned on a WHERE clause.
# These aren't surfaced by `pg_constraint` (only unconditional UNIQUE
# constraints are constraint rows); they live in `pg_index` with
# `indpred IS NOT NULL`. The non-partial form (`CREATE UNIQUE INDEX
# ... ON t (col)` with no WHERE) is intentionally excluded — that
# case isn't picked up by sqlproof today and is out of scope for the
# partial-unique work; the inline `UNIQUE` constraint remains the
# canonical spelling for unconditional uniques.
_PARTIAL_UNIQUE_SQL = """
SELECT
  ns.nspname AS schema_name,
  cls.relname AS table_name,
  array_agg(att.attname ORDER BY key_column.ordinality) AS columns,
  pg_get_expr(idx.indpred, idx.indrelid, true) AS predicate
FROM pg_catalog.pg_index idx
JOIN pg_catalog.pg_class cls ON cls.oid = idx.indrelid
JOIN pg_catalog.pg_namespace ns ON ns.oid = cls.relnamespace
JOIN unnest(idx.indkey) WITH ORDINALITY AS key_column(attnum, ordinality) ON true
JOIN pg_catalog.pg_attribute att
  ON att.attrelid = cls.oid AND att.attnum = key_column.attnum
WHERE ns.nspname = %s
  AND idx.indisunique
  AND idx.indpred IS NOT NULL
GROUP BY ns.nspname, cls.relname, idx.indexrelid, idx.indpred, idx.indrelid
ORDER BY ns.nspname, cls.relname
"""

# Exclusion constraints — `EXCLUDE USING <am> (col WITH <op>, ...)`.
# Postgres stores the column list in `con.conkey`, the operator OIDs
# in `con.conexclop`, and the index AM via the backing index's
# `pg_class.relam`. The two `unnest WITH ORDINALITY` joins below
# pair columns with operators positionally.
_EXCLUSIONS_SQL = """
SELECT
  ns.nspname AS schema_name,
  cls.relname AS table_name,
  am.amname AS access_method,
  array_agg(att.attname ORDER BY ordinality) AS columns,
  array_agg(op.oprname ORDER BY ordinality) AS operators
FROM pg_catalog.pg_constraint con
JOIN pg_catalog.pg_class cls ON cls.oid = con.conrelid
JOIN pg_catalog.pg_namespace ns ON ns.oid = cls.relnamespace
JOIN pg_catalog.pg_class idx_cls ON idx_cls.oid = con.conindid
JOIN pg_catalog.pg_am am ON am.oid = idx_cls.relam
JOIN unnest(con.conkey) WITH ORDINALITY AS k(attnum, ordinality) ON true
JOIN pg_catalog.pg_attribute att
  ON att.attrelid = cls.oid AND att.attnum = k.attnum
JOIN unnest(con.conexclop) WITH ORDINALITY AS o(oprid, ordinality)
  USING (ordinality)
JOIN pg_catalog.pg_operator op ON op.oid = o.oprid
WHERE ns.nspname = %s AND con.contype = 'x'
GROUP BY ns.nspname, cls.relname, am.amname, con.oid
ORDER BY ns.nspname, cls.relname, con.conname
"""
