from __future__ import annotations

import re

from sqlproof.exceptions import SqlProofSchemaError
from sqlproof.schema.model import CheckConstraint, Column, ForeignKey, PgType, SchemaInfo, Table

_CREATE_TYPE_RE = re.compile(
    r"CREATE\s+TYPE\s+(?P<name>[a-zA-Z_][\w.]*)\s+AS\s+ENUM\s*\((?P<values>.*?)\)",
    re.IGNORECASE | re.DOTALL,
)
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?P<name>[a-zA-Z_][\w.]*)\s*\((?P<body>.*?)\)\s*;",
    re.IGNORECASE | re.DOTALL,
)


def parse_schema_sql(sql: str, *, schema: str = "public") -> SchemaInfo:
    enums = _parse_enums(sql)
    enum_names = {enum.name: enum for enum in enums}
    tables = tuple(
        _parse_table(match, enum_names, schema) for match in _CREATE_TABLE_RE.finditer(sql)
    )
    if not tables and "CREATE TABLE" in sql.upper():
        raise SqlProofSchemaError("Could not parse CREATE TABLE statement.")
    return SchemaInfo(tables=tables, enums=enums)


def _parse_enums(sql: str) -> tuple[PgType, ...]:
    enums: list[PgType] = []
    for match in _CREATE_TYPE_RE.finditer(sql):
        values = tuple(value.strip().strip("'\"") for value in match.group("values").split(","))
        enums.append(PgType(kind="enum", name=_unqualify(match.group("name")), enum_values=values))
    return tuple(enums)


def _parse_table(match: re.Match[str], enum_names: dict[str, PgType], schema: str) -> Table:
    raw_name = match.group("name")
    table_schema, table_name = _split_qualified(raw_name, default_schema=schema)
    columns: list[Column] = []
    primary_key: tuple[str, ...] = ()
    foreign_keys: list[ForeignKey] = []
    unique_constraints: list[tuple[str, ...]] = []
    check_constraints: list[CheckConstraint] = []

    for item in _split_top_level_commas(match.group("body")):
        text = item.strip()
        upper = text.upper()
        if upper.startswith("PRIMARY KEY"):
            primary_key = tuple(_parse_column_list(text))
            continue
        if upper.startswith("UNIQUE"):
            unique_constraints.append(tuple(_parse_column_list(text)))
            continue
        if upper.startswith("FOREIGN KEY"):
            foreign_keys.append(_parse_table_fk(text))
            continue
        if upper.startswith("CHECK"):
            check_constraints.append(CheckConstraint(_inside_parentheses(text)))
            continue

        column = _parse_column(text, enum_names)
        columns.append(column)
        if "PRIMARY KEY" in upper:
            primary_key = (column.name,)
        if "UNIQUE" in upper:
            unique_constraints.append((column.name,))
        fk = _parse_inline_fk(column.name, text)
        if fk is not None:
            foreign_keys.append(fk)
        check = _parse_inline_check(text)
        if check is not None:
            check_constraints.append(check)

    return Table(
        schema=table_schema,
        name=table_name,
        columns=tuple(columns),
        primary_key=primary_key,
        foreign_keys=tuple(foreign_keys),
        unique_constraints=tuple(unique_constraints),
        check_constraints=tuple(check_constraints),
    )


def _parse_column(text: str, enum_names: dict[str, PgType]) -> Column:
    parts = text.split()
    if len(parts) < 2:
        raise SqlProofSchemaError(f"Could not parse column definition: {text}")
    name = _clean_identifier(parts[0])
    type_tokens: list[str] = []
    for token in parts[1:]:
        if token.upper() in {
            "NOT",
            "NULL",
            "DEFAULT",
            "PRIMARY",
            "REFERENCES",
            "UNIQUE",
            "CHECK",
            "GENERATED",
            "IDENTITY",
        }:
            break
        type_tokens.append(token)
    type_sql = " ".join(type_tokens)
    pg_type = _parse_type(type_sql, enum_names)
    upper = text.upper()
    return Column(
        name=name,
        type=pg_type,
        nullable="NOT NULL" not in upper and "PRIMARY KEY" not in upper,
        default=_parse_default(text),
        is_generated=pg_type.name in {"serial", "bigserial"} or "GENERATED" in upper,
    )


def _parse_type(type_sql: str, enum_names: dict[str, PgType]) -> PgType:
    normalized = type_sql.strip().lower()
    bare = re.sub(r"\s+", " ", normalized)
    if bare in enum_names:
        return enum_names[bare]
    modifier_match = re.match(r"(?P<name>\w+)\((?P<mods>[\d,\s]+)\)", bare)
    if modifier_match:
        modifiers = tuple(int(part.strip()) for part in modifier_match.group("mods").split(","))
        return PgType(kind="scalar", name=modifier_match.group("name"), modifiers=modifiers)
    return PgType(kind="scalar", name=bare)


def _parse_default(text: str) -> str | None:
    match = re.search(
        r"\bDEFAULT\s+(.+?)(?:\s+NOT\s+NULL|\s+PRIMARY\s+KEY|\s+UNIQUE|\s+CHECK|\s+REFERENCES|$)",
        text,
        re.I,
    )
    return match.group(1).strip() if match else None


def _parse_inline_fk(column_name: str, text: str) -> ForeignKey | None:
    match = re.search(r"\bREFERENCES\s+([a-zA-Z_][\w.]*)\s*\(([^)]+)\)", text, re.I)
    if match is None:
        return None
    return ForeignKey(
        columns=(column_name,),
        referenced_table=_unqualify(match.group(1)),
        referenced_columns=tuple(_clean_identifier(part) for part in match.group(2).split(",")),
        on_delete="NO ACTION",
        on_update="NO ACTION",
    )


def _parse_table_fk(text: str) -> ForeignKey:
    source = tuple(_parse_column_list(text))
    match = re.search(r"\bREFERENCES\s+([a-zA-Z_][\w.]*)\s*\(([^)]+)\)", text, re.I)
    if match is None:
        raise SqlProofSchemaError(f"Could not parse foreign key: {text}")
    return ForeignKey(
        columns=source,
        referenced_table=_unqualify(match.group(1)),
        referenced_columns=tuple(_clean_identifier(part) for part in match.group(2).split(",")),
        on_delete="NO ACTION",
        on_update="NO ACTION",
    )


def _parse_inline_check(text: str) -> CheckConstraint | None:
    match = re.search(r"\bCHECK\s*\((?P<expr>.*)\)\s*$", text, re.I)
    if match is None:
        return None
    return CheckConstraint(match.group("expr").strip())


def _split_top_level_commas(body: str) -> list[str]:
    items: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(body):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            items.append(body[start:index])
            start = index + 1
    items.append(body[start:])
    return [item for item in items if item.strip()]


def _parse_column_list(text: str) -> tuple[str, ...]:
    inside = _inside_parentheses(text)
    return tuple(_clean_identifier(part) for part in inside.split(","))


def _inside_parentheses(text: str) -> str:
    start = text.index("(") + 1
    end = text.rindex(")")
    return text[start:end].strip()


def _split_qualified(name: str, *, default_schema: str) -> tuple[str, str]:
    parts = [_clean_identifier(part) for part in name.split(".")]
    if len(parts) == 2:
        return parts[0], parts[1]
    return default_schema, parts[0]


def _unqualify(name: str) -> str:
    return _split_qualified(name, default_schema="public")[1]


def _clean_identifier(identifier: str) -> str:
    return identifier.strip().strip('"').lower()
