"""A counterexample as a scenario, so a failing dataset can be reloaded.

`sqlproof replay FILE --as-scenario` writes the dataset of a counterexample
file in the serialized scenario form; `sqlproof scenarios up` then loads it
into a database for a person to inspect:

* every row becomes a pinned row: a named row (`row: orders_1`) in a table
  with a single-column primary key, a one-row cohort otherwise;
* every column the dataset holds is pinned, except generated columns and
  a single-column primary key, which the scenario assigns -- unless that
  key is also a foreign key (`profiles.id -> auth.users`), which is pinned
  as a reference like any other;
* a foreign key to a row in the dataset becomes a reference to that row
  (`{ref: customers_2}`), so relationships survive the new keys; one to
  `auth.users` becomes a reference to a seeded auth user.

Primary keys are reassigned in row order, so a property that depends on
the key values themselves, not on the relationships, may not fail the
same way. The counterexample keeps the original rows for that.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from sqlproof.exceptions import SqlProofScenarioError
from sqlproof.scenarios.model import Scenario
from sqlproof.schema.model import Column, ForeignKey, PgType, SchemaInfo, Table

_AUTH_USERS = "auth.users"
_UNSAFE = re.compile(r"[^A-Za-z0-9_]+")


def scenario_from_counterexample(
    payload: Mapping[str, Any], schema: SchemaInfo, *, name: str | None = None
) -> Scenario:
    """The scenario that loads `payload`'s dataset (see the module docstring).

    `payload` is a counterexample file's content; `schema` the schema the
    property ran against."""
    from sqlproof.scenarios.serialize import scenario_from_dict

    dataset = payload.get("dataset")
    if not isinstance(dataset, Mapping):
        msg = "the counterexample has no dataset to export."
        raise SqlProofScenarioError(msg)
    rows_by_table = _rows_by_table(dataset, schema)  # pyright: ignore[reportUnknownArgumentType]

    duplicate_names = _duplicate_bare_names(schema)
    # (table, referenced column, value) -> row name, for references.
    keys: dict[tuple[str, str, str], str] = {}
    names: dict[str, list[str | None]] = {}
    for table, rows in rows_by_table:
        if len(table.primary_key) != 1:
            names[table.qualified_name] = [None] * len(rows)
            continue
        pk = table.primary_key[0]
        prefix = _row_prefix(table, duplicate_names)
        table_names: list[str | None] = []
        for index, row in enumerate(rows):
            row_name = f"{prefix}_{index + 1}"
            table_names.append(row_name)
            if pk in row:
                keys[(table.qualified_name, pk, _key(row[pk]))] = row_name
        names[table.qualified_name] = table_names

    auth_users: dict[str, str] = {}
    tables: dict[str, list[dict[str, Any]]] = {}
    for table, rows in rows_by_table:
        groups: list[dict[str, Any]] = []
        for index, row in enumerate(rows):
            pinned: dict[str, Any] = {}
            for column in table.columns:
                if column.name not in row or column.is_generated:
                    continue
                # The scenario assigns a plain key; one that is also a
                # foreign key is pinned, as a reference to its parent row.
                if table.primary_key == (column.name,) and (
                    _single_column_fk(table, column.name) is None
                ):
                    continue
                pinned[column.name] = _pinned_value(
                    table, column, row[column.name], keys, auth_users
                )
            label = names[table.qualified_name][index]
            group: dict[str, Any] = {"row": label} if label is not None else {"cohort": 1}
            if pinned:
                group["set"] = pinned
            groups.append(group)
        tables[_table_key(table, duplicate_names)] = groups
    if auth_users:
        tables = {_AUTH_USERS: [{"row": n} for n in auth_users.values()], **tables}

    seed = payload.get("seed")
    data: dict[str, Any] = {
        "version": 1,
        "name": name or _scenario_name(payload),
        "seed": seed if isinstance(seed, int) and not isinstance(seed, bool) else 0,
        "tables": tables,
    }
    return scenario_from_dict(data)


def _rows_by_table(
    dataset: Mapping[str, Any], schema: SchemaInfo
) -> list[tuple[Table, list[dict[str, Any]]]]:
    """The dataset's tables in schema order, with their rows."""
    by_key: dict[str, Table] = {}
    for table in schema.tables:
        by_key[table.qualified_name] = table
        by_key.setdefault(table.name, table)
    found: dict[str, list[dict[str, Any]]] = {}
    for key, rows in dataset.items():
        match = by_key.get(str(key))
        if match is not None and match.qualified_name == _AUTH_USERS:
            continue
        if match is None:
            if str(key) in (_AUTH_USERS, "users"):
                continue  # auth users are recreated from the references to them
            known = ", ".join(sorted(t.qualified_name for t in schema.tables)) or "none"
            msg = (
                f"the counterexample has rows for {key!r}, which the schema does not "
                f"model ({known}). Was it recorded against another schema?"
            )
            raise SqlProofScenarioError(msg)
        if not isinstance(rows, list):
            msg = f"the counterexample's rows for {key!r} are not a list."
            raise SqlProofScenarioError(msg)
        found[match.qualified_name] = [
            dict(row)  # pyright: ignore[reportUnknownArgumentType]
            for row in rows  # pyright: ignore[reportUnknownVariableType]
            if isinstance(row, Mapping)
        ]
    return [(t, found[t.qualified_name]) for t in schema.tables if t.qualified_name in found]


def _pinned_value(
    table: Table,
    column: Column,
    value: Any,
    keys: Mapping[tuple[str, str, str], str],
    auth_users: dict[str, str],
) -> Any:
    if value is None:
        return None
    fk = _single_column_fk(table, column.name)
    if fk is not None:
        if fk.referenced_schema == "auth" and fk.referenced_table == "users":
            user = auth_users.setdefault(_key(value), f"user_{len(auth_users) + 1}")
            return {"ref": user}
        target = _referenced_table(fk, table)
        row_name = keys.get((target, fk.referenced_columns[0], _key(value)))
        if row_name is not None:
            return {"ref": row_name}
    return _literal(column.type, value)


def _literal(pg_type: PgType, value: Any) -> Any:
    """`value`, as JSON loaded it, in a form the scenario loads back as the
    same Postgres value."""
    base = pg_type
    while base.kind == "domain" and base.base is not None:
        base = base.base
    if base.name in ("json", "jsonb"):
        return value
    if isinstance(value, Mapping):
        mapping: Mapping[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        if mapping.get("__type__") == "Range":
            return _range_literal(mapping)
        return json.dumps(mapping, sort_keys=True)
    if base.name == "interval" and isinstance(value, int | float):
        return f"{value} seconds"
    if isinstance(value, list):
        items: list[Any] = value  # pyright: ignore[reportUnknownVariableType]
        if base.name in ("vector", "halfvec"):
            return "[" + ",".join(str(item) for item in items) + "]"
        if base.kind == "composite":
            return "(" + ",".join("" if item is None else _quoted(item) for item in items) + ")"
        if base.kind == "array" and base.base is not None:
            return [_literal(base.base, item) if item is not None else None for item in items]
        return items
    return value


def _range_literal(value: Mapping[str, Any]) -> str:
    bounds = str(value.get("bounds") or "[)")
    lower = value.get("lower")
    upper = value.get("upper")
    return (
        f"{bounds[0]}{'' if lower is None else _quoted(lower)},"
        f"{'' if upper is None else _quoted(upper)}{bounds[-1]}"
    )


def _quoted(value: Any) -> str:
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _single_column_fk(table: Table, column: str) -> ForeignKey | None:
    for fk in table.foreign_keys:
        if fk.columns == (column,):
            return fk
    return None


def _referenced_table(fk: ForeignKey, table: Table) -> str:
    schema = fk.referenced_schema or table.schema
    return f"{schema}.{fk.referenced_table}"


def _key(value: Any) -> str:
    """Values as JSON wrote them compare by their text: a key read back
    from a file is a string where the original was a UUID or a Decimal."""
    return json.dumps(value, sort_keys=True) if isinstance(value, list | dict) else str(value)


def _duplicate_bare_names(schema: SchemaInfo) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for table in schema.tables:
        (duplicates if table.name in seen else seen).add(table.name)
    return duplicates


def _table_key(table: Table, duplicates: set[str]) -> str:
    return table.qualified_name if table.name in duplicates else table.name


def _row_prefix(table: Table, duplicates: set[str]) -> str:
    raw = f"{table.schema}_{table.name}" if table.name in duplicates else table.name
    return _UNSAFE.sub("_", raw)


def _scenario_name(payload: Mapping[str, Any]) -> str:
    prop = str(payload.get("property_name") or "property")
    return "counterexample_" + _UNSAFE.sub("_", prop).strip("_")
