"""Compile a scenario against a schema into what the bulk loader takes.

The output is the generators' existing vocabulary -- `sizes` keyed by
qualified table name, and one row-index override per affected column --
plus the resolved keys of named rows and the `auth.users` rows to seed.
Nothing here touches a database, so every error a scenario can carry
that is visible in the scenario and schema alone is raised here, before
any load.

Row layout: a table's groups are laid out in declaration order, so each
group owns a fixed index range. An override looks up which group a row
index falls in and returns that group's pinned value, or `GENERATE` to
hand the column back to the generator. Keys are a pure function of row
index (`_unique_value`), which is why a reference to a named row, and a
child row's parent under `per_parent`, can be resolved before any row
exists.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import random
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, cast

from sqlproof._database import Json
from sqlproof.core import _base_type_name  # pyright: ignore[reportPrivateUsage]
from sqlproof.exceptions import SqlProofGenerationError, SqlProofScenarioError
from sqlproof.generators.rows import (
    GENERATE,
    PinnedKey,
    _foreign_key_for_column,  # pyright: ignore[reportPrivateUsage]
    _is_single_column_unique,  # pyright: ignore[reportPrivateUsage]
    _unique_value,  # pyright: ignore[reportPrivateUsage]
)
from sqlproof.scale.load import insertion_plan, missing_required_parents
from sqlproof.scenarios.model import Group, PerParent, RowRef, TableSpec
from sqlproof.schema.checks import parse_check_expression
from sqlproof.schema.fingerprint import compute
from sqlproof.schema.model import Column, ForeignKey, ParsedCheck, SchemaInfo, Table

if TYPE_CHECKING:
    from sqlproof.generators.bulk import BulkColumnContext
    from sqlproof.scenarios.model import Scenario

AUTH_USERS = "auth.users"

_CLOCK_DEFAULT = re.compile(
    r"\b(now\s*\(\s*\)|current_timestamp|clock_timestamp\s*\(\s*\)|"
    r"statement_timestamp\s*\(\s*\)|transaction_timestamp\s*\(\s*\)|"
    r"localtimestamp|current_date)",
    re.IGNORECASE,
)
_UUID_DEFAULT = re.compile(r"\b(gen_random_uuid|uuid_generate_v[14]\w*)\s*\(", re.IGNORECASE)
_RANDOM_DEFAULT = re.compile(r"\brandom\s*\(\s*\)", re.IGNORECASE)
_TIMESTAMPTZ = {"timestamptz", "timestamp with time zone"}
_TIMESTAMP = {"timestamp", "timestamp without time zone"}
_FLOAT = {"float8", "double precision", "float4", "real"}


@dataclass(frozen=True, slots=True)
class AuthUser:
    """One `auth.users` row a scenario seeds."""

    id: str
    email: str
    name: str | None


@dataclass(frozen=True, slots=True)
class CompiledScenario:
    name: str
    seed: int
    clock: datetime
    sizes: dict[str, int]
    columns: dict[str, Any]
    refs: dict[str, Any]
    auth_users: tuple[AuthUser, ...]
    fingerprint: str
    schema_fingerprint: str
    nondeterministic_defaults: tuple[str, ...]


@dataclass(slots=True)
class _Layout:
    qualified: str
    table: Table | None  # None for auth.users
    groups: tuple[Group, ...]
    starts: list[int]
    size: int

    def group_index(self, row_index: int) -> int:
        return bisect.bisect_right(self.starts, row_index) - 1

    def indexes(self, group_index: int) -> range:
        start = self.starts[group_index]
        return range(start, start + self.groups[group_index].count)


def _digest(*parts: object) -> bytes:
    return hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).digest()


def _uuid(*parts: object) -> str:
    return str(uuid.UUID(bytes=_digest(*parts)[:16], version=4))


def _unit(*parts: object) -> float:
    return int.from_bytes(_digest(*parts)[:8], "big") / 2**64


def compile_scenario(scenario: Scenario, schema: SchemaInfo) -> CompiledScenario:
    spec = scenario.spec
    where = f"scenario {spec.name!r}"
    specs_by_target: dict[str, TableSpec] = {}
    tables_by_target: dict[str, Table | None] = {}
    for table_spec in spec.tables:
        target, table = _resolve_table(table_spec.name, schema, where)
        if target in specs_by_target:
            msg = (
                f"{where}: table {target} is declared twice (as "
                f"{specs_by_target[target].name!r} and {table_spec.name!r})."
            )
            raise SqlProofScenarioError(msg)
        specs_by_target[target] = table_spec
        tables_by_target[target] = table

    # After resolving names, so an ambiguous or unknown table is reported
    # as such rather than as whatever the dependency graph makes of it.
    try:
        plan = insertion_plan(schema)
    except SqlProofGenerationError as exc:
        msg = f"{where}: {exc}"
        raise SqlProofScenarioError(msg) from exc

    layouts: dict[str, _Layout] = {}
    named: dict[str, tuple[str, int]] = {}
    refs: dict[str, Any] = {}
    auth_users: list[AuthUser] = []

    # auth.users first: other tables reference it.
    if AUTH_USERS in specs_by_target:
        layout = _group_layout(AUTH_USERS, None, specs_by_target[AUTH_USERS], where)
        layouts[AUTH_USERS] = layout
        for group_index, group in enumerate(layout.groups):
            if group.pinned:
                msg = (
                    f"{where}: auth.users rows cannot pin columns; only fill(n) and "
                    "row('auth.users', name=...) are supported."
                )
                raise SqlProofScenarioError(msg)
            for index in layout.indexes(group_index):
                user_id = _uuid(spec.name, spec.seed, AUTH_USERS, index)
                auth_users.append(
                    AuthUser(
                        id=user_id,
                        email=f"sqlproof+{spec.name}+{index}@test.invalid",
                        name=group.name,
                    )
                )
                if group.name is not None:
                    named[group.name] = (AUTH_USERS, index)
                    refs[group.name] = user_id

    # Modelled tables with groups, in insertion order, so named rows have
    # keys before anything references them.
    # auth.users was laid out above, even when the schema models it.
    ordered = [
        t
        for t in plan.ordered_tables
        if t.qualified_name in specs_by_target and t.qualified_name != AUTH_USERS
    ]
    for table in ordered:
        table_spec = specs_by_target[table.qualified_name]
        if table_spec.per_parent is not None:
            continue
        layout = _group_layout(table.qualified_name, table, table_spec, where)
        layouts[table.qualified_name] = layout
        for group_index, group in enumerate(layout.groups):
            if group.name is None:
                continue
            index = layout.indexes(group_index)[0]
            named[group.name] = (table.qualified_name, index)
            pinned_key = (
                group.pinned.get(table.primary_key[0]) if len(table.primary_key) == 1 else None
            )
            if isinstance(pinned_key, RowRef) and pinned_key.name in refs:
                # Its key is the referenced row's key (parents come first).
                refs[group.name] = refs[pinned_key.name]
            else:
                refs[group.name] = _key_value(table, index, group.name, where)

    # Per-parent tables: sized from their parent's layout.
    parent_keys: dict[tuple[str, str], list[Any]] = {}
    for table in ordered:
        table_spec = specs_by_target[table.qualified_name]
        pp = table_spec.per_parent
        if pp is None:
            continue
        if table_spec.groups:
            msg = (
                f"{where}: {table.qualified_name} uses per_parent, which sizes the "
                "table itself; it cannot also have cohorts, rows or fills."
            )
            raise SqlProofScenarioError(msg)
        column, keys = _per_parent_layout(
            table, pp, layouts, named, schema, spec.name, spec.seed, where
        )
        layouts[table.qualified_name] = _Layout(
            table.qualified_name, table, (Group("fill", len(keys)),), [0], len(keys)
        )
        parent_keys[(table.qualified_name, column.name)] = keys

    _check_children_of_pinned_keys(layouts, where)

    sizes = {
        qualified: layout.size for qualified, layout in layouts.items() if qualified != AUTH_USERS
    }
    # A schema file may model auth.users itself; its rows come from the
    # scenario's auth users, not from the generator.
    missing = missing_required_parents(schema, {**sizes, AUTH_USERS: len(auth_users)})
    if missing:
        msg = (
            f"{where}: these rows would have no parent row to reference: "
            f"{'; '.join(missing)}. Give each parent table rows (for example "
            "s.table('orgs').fill(3)) or leave the child table out."
        )
        raise SqlProofScenarioError(msg)

    auth_ids = [user.id for user in auth_users]
    columns: dict[str, Any] = {}
    nondeterministic: list[str] = []
    for qualified, layout in layouts.items():
        table = layout.table
        if table is None:
            continue
        for column in table.columns:
            override = _column_override_for(
                layout,
                column,
                refs=refs,
                named=named,
                parent_keys=parent_keys.get((qualified, column.name)),
                auth_ids=auth_ids,
                scenario_name=spec.name,
                seed=spec.seed,
                clock=spec.clock,
                nondeterministic=nondeterministic,
                where=where,
            )
            if override is not None:
                columns[f"{qualified}.{column.name}"] = override

    return CompiledScenario(
        name=spec.name,
        seed=spec.seed,
        clock=spec.clock,
        sizes=sizes,
        columns=columns,
        refs=refs,
        auth_users=tuple(auth_users),
        fingerprint=scenario.fingerprint,
        schema_fingerprint=compute(schema),
        nondeterministic_defaults=tuple(nondeterministic),
    )


def _resolve_table(name: str, schema: SchemaInfo, where: str) -> tuple[str, Table | None]:
    if "." in name:
        schema_name, table_name = name.split(".", 1)
        matches = [t for t in schema.tables if t.schema == schema_name and t.name == table_name]
    else:
        schema_name, table_name = "", name
        matches = [t for t in schema.tables if t.name == name]
    if len(matches) == 1:
        return matches[0].qualified_name, matches[0]
    if len(matches) > 1:
        options = ", ".join(t.qualified_name for t in matches)
        msg = f"{where}: table {name!r} is ambiguous ({options}); use the qualified name."
        raise SqlProofScenarioError(msg)
    if (schema_name, table_name) == ("auth", "users"):
        return AUTH_USERS, None
    known = ", ".join(sorted(t.qualified_name for t in schema.tables)) or "none"
    hint = " For Supabase users, use 'auth.users'." if table_name == "users" else ""
    msg = f"{where}: unknown table {name!r}. Modelled tables: {known}.{hint}"
    raise SqlProofScenarioError(msg)


def _group_layout(qualified: str, table: Table | None, spec: TableSpec, where: str) -> _Layout:
    starts: list[int] = []
    total = 0
    for group in spec.groups:
        starts.append(total)
        total += group.count
    if table is not None:
        for group in spec.groups:
            for column_name in group.pinned:
                _check_pinnable(table, column_name, where)
        _check_key_pins(table, spec.groups, where)
    return _Layout(qualified, table, spec.groups, starts, total)


def _check_children_of_pinned_keys(layouts: Mapping[str, _Layout], where: str) -> None:
    """Rows that reference a table whose key is pinned must reference it by
    name: the generator picks parents by index arithmetic over generated
    keys, which a pinned key does not follow."""
    pinned = {
        qualified
        for qualified, layout in layouts.items()
        if layout.table is not None
        and len(layout.table.primary_key) == 1
        and any(layout.table.primary_key[0] in g.pinned for g in layout.groups)
    }
    if not pinned:
        return
    for qualified, layout in layouts.items():
        table = layout.table
        if table is None:
            continue
        for fk in table.foreign_keys:
            if len(fk.columns) != 1:
                continue
            target = f"{fk.referenced_schema or table.schema}.{fk.referenced_table}"
            if target not in pinned or (target == qualified and fk.columns[0] in table.primary_key):
                continue
            column = fk.columns[0]
            if not all(column in g.pinned for g in layout.groups) or not layout.groups:
                msg = (
                    f"{where}: {target}'s primary key is pinned, so rows of {qualified} "
                    f"must pin {column} to a named row of {target} ({{ref: <row>}})."
                )
                raise SqlProofScenarioError(msg)


def _check_key_pins(table: Table, groups: Sequence[Group], where: str) -> None:
    """A pinned primary key must be pinned on every row, whole and distinct.

    The generator assigns keys from the row index, so a key pinned on some
    rows and generated on others, pinned in part, or pinned on a cohort of
    several rows, would collide at load time. Refuse here instead."""
    key = table.primary_key
    if not key or not any(column in group.pinned for group in groups for column in key):
        return
    qualified = f"{table.qualified_name}({', '.join(key)})"
    seen: set[tuple[str, ...]] = set()
    for group in groups:
        missing = [column for column in key if column not in group.pinned]
        if missing or group.count != 1:
            msg = (
                f"{where}: the primary key {qualified} is pinned, so every row of "
                f"{table.qualified_name} must pin all of it, one row per group "
                "(row(...) or cohort(1, ...)); the generator would otherwise assign "
                "colliding keys."
            )
            raise SqlProofScenarioError(msg)
        values = tuple(
            f"ref:{value.name}" if isinstance(value, RowRef) else f"lit:{value!r}"
            for value in (group.pinned[column] for column in key)
        )
        if values in seen:
            msg = f"{where}: two rows of {table.qualified_name} pin the same key {qualified}."
            raise SqlProofScenarioError(msg)
        seen.add(values)
    if len(key) == 1:
        for group in groups:
            if not isinstance(group.pinned[key[0]], RowRef):
                msg = (
                    f"{where}: {table.qualified_name}.{key[0]} is a primary key and a "
                    "foreign key; pin it to a reference ({ref: <row>}), not a literal."
                )
                raise SqlProofScenarioError(msg)


def _check_pinnable(table: Table, column_name: str, where: str) -> Column:
    names = {column.name for column in table.columns}
    if column_name not in names:
        msg = (
            f"{where}: {table.qualified_name} has no column {column_name!r}. "
            f"Columns: {', '.join(sorted(names))}."
        )
        raise SqlProofScenarioError(msg)
    column = table.column(column_name)
    # Key columns are pinnable only where the key is made of references: a
    # join table's composite key, or a single-column key that is also a
    # foreign key (`profiles.id -> auth.users`). `_check_key_pins` then
    # requires every row of the table to pin the whole key, distinctly.
    if table.primary_key == (column_name,) and _foreign_key_for_column(table, column_name) is None:
        msg = (
            f"{where}: {table.qualified_name}.{column_name} is part of the primary "
            "key, which the generator assigns; name the row with s.row(...) and "
            "reference it instead of pinning its key."
        )
        raise SqlProofScenarioError(msg)
    if column.is_generated:
        msg = (
            f"{where}: {table.qualified_name}.{column_name} is generated by "
            "Postgres and cannot be pinned."
        )
        raise SqlProofScenarioError(msg)
    return column


def _key_value(table: Table, index: int, row_name: str, where: str) -> Any:
    if len(table.primary_key) != 1:
        msg = (
            f"{where}: row {row_name!r} is in {table.qualified_name}, which has no "
            "single-column primary key, so it cannot be referenced."
        )
        raise SqlProofScenarioError(msg)
    pk = table.primary_key[0]
    return _unique_value(pk, table.column(pk).type.name, index)


def _fk_into(table: Table, parent: str, column: str | None, where: str) -> ForeignKey:
    parent_schema, parent_name = parent.split(".", 1)
    candidates = [
        fk
        for fk in table.foreign_keys
        if len(fk.columns) == 1
        and fk.referenced_table == parent_name
        and fk.referenced_schema in (None, parent_schema)
        and (column is None or fk.columns[0] == column)
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        msg = f"{where}: {table.qualified_name} has no single-column foreign key into {parent}."
    else:
        options = ", ".join(fk.columns[0] for fk in candidates)
        msg = (
            f"{where}: {table.qualified_name} has several foreign keys into {parent} "
            f"({options}); pass per_parent(..., column=...) to choose one."
        )
    raise SqlProofScenarioError(msg)


def _per_parent_layout(
    table: Table,
    pp: PerParent,
    layouts: Mapping[str, _Layout],
    named: Mapping[str, tuple[str, int]],
    schema: SchemaInfo,
    scenario_name: str,
    seed: int,
    where: str,
) -> tuple[Column, list[Any]]:
    parent_target, _parent_table = _resolve_table(pp.parent, schema, where)
    parent_layout = layouts.get(parent_target)
    if parent_layout is None or parent_layout.size == 0:
        msg = (
            f"{where}: {table.qualified_name} is sized per {parent_target}, which has "
            "no rows in this scenario. Declare the parent's rows first."
        )
        raise SqlProofScenarioError(msg)
    fk = _fk_into(table, parent_target, pp.column, where)
    column = table.column(fk.columns[0])
    matched = _matching_parents(parent_layout, pp.where, named, where)
    rng = random.Random(f"{scenario_name}:{seed}:per_parent:{table.qualified_name}")
    keys: list[Any] = []
    parent_table = parent_layout.table
    for parent_index in range(parent_layout.size):
        low, high = pp.count if matched[parent_index] else pp.default
        children = rng.randint(low, high)
        if parent_table is None:
            key: Any = _uuid(scenario_name, seed, AUTH_USERS, parent_index)
        else:
            key = _unique_value(fk.referenced_columns[0], column.type.name, parent_index)
        keys.extend([key] * children)
    return column, keys


def _matching_parents(
    layout: _Layout,
    where_clause: Mapping[str, Any] | RowRef | None,
    named: Mapping[str, tuple[str, int]],
    where: str,
) -> list[bool]:
    if where_clause is None:
        return [True] * layout.size
    if isinstance(where_clause, RowRef):
        target = named.get(where_clause.name)
        if target is None or target[0] != layout.qualified:
            msg = (
                f"{where}: per_parent(where={where_clause!r}) must name a row of "
                f"{layout.qualified}."
            )
            raise SqlProofScenarioError(msg)
        return [index == target[1] for index in range(layout.size)]
    pinned_anywhere = {name for group in layout.groups for name in group.pinned}
    unknown = sorted(set(where_clause) - pinned_anywhere)
    if unknown:
        msg = (
            f"{where}: per_parent(where=...) can only use columns {layout.qualified}'s "
            f"cohorts pin; {', '.join(unknown)} is not pinned by any of them."
        )
        raise SqlProofScenarioError(msg)
    matched: list[bool] = []
    for group in layout.groups:
        hit = all(
            key in group.pinned and group.pinned[key] == value
            for key, value in where_clause.items()
        )
        matched.extend([hit] * group.count)
    return matched


_UNSET = object()


def _column_override_for(
    layout: _Layout,
    column: Column,
    *,
    refs: Mapping[str, Any],
    named: Mapping[str, tuple[str, int]],
    parent_keys: Sequence[Any] | None,
    auth_ids: Sequence[str],
    scenario_name: str,
    seed: int,
    clock: datetime,
    nondeterministic: list[str],
    where: str,
) -> Callable[[BulkColumnContext], Any] | None:
    table = layout.table
    assert table is not None
    single_key = column.name in table.primary_key and len(table.primary_key) == 1
    if single_key and not any(column.name in group.pinned for group in layout.groups):
        return None
    if column.is_generated:
        return None
    qualified_column = f"{layout.qualified}.{column.name}"
    fk = _foreign_key_for_column(table, column.name)

    pinned: list[Any] = []
    for group in layout.groups:
        if column.name not in group.pinned:
            pinned.append(_UNSET)
            continue
        value = group.pinned[column.name]
        if isinstance(value, RowRef):
            value = _resolve_ref(value, fk, refs, named, qualified_column, where)
        elif not callable(value):
            _check_literal(table, column, value, where)
        pinned.append(value)
    if any(value is not _UNSET for value in pinned):
        _check_unique_collisions(layout, column, pinned, where)

    unpinned_rows = sum(
        group.count for group, value in zip(layout.groups, pinned, strict=True) if value is _UNSET
    )
    auth_pick: Sequence[str] | None = None
    if fk is not None and parent_keys is None and _targets_auth_users(fk) and unpinned_rows:
        if auth_ids:
            auth_pick = auth_ids
        elif not column.nullable:
            msg = (
                f"{where}: {qualified_column} must reference an auth.users row, and the "
                "scenario seeds none. Add s.table('auth.users').fill(n) or pin the column."
            )
            raise SqlProofScenarioError(msg)

    volatile = _volatile_default(column, scenario_name, seed, layout.qualified, clock)
    if column.default is not None and volatile is None and _looks_volatile(column.default):
        nondeterministic.append(f"{qualified_column}: {column.default}")

    has_pins = any(value is not _UNSET for value in pinned)
    if not (has_pins or parent_keys is not None or auth_pick or volatile):
        return None

    def override(context: BulkColumnContext) -> Any:
        index = context.row_index
        if has_pins:
            value = pinned[layout.group_index(index)]
            if value is not _UNSET:
                return value(context) if callable(value) else value
        if parent_keys is not None:
            return parent_keys[index]
        if auth_pick:
            position = int(_unit(scenario_name, seed, qualified_column, index) * len(auth_pick))
            return auth_pick[position]
        if volatile is not None:
            return volatile(index)
        return GENERATE

    return PinnedKey(override) if single_key else override


def _targets_auth_users(fk: ForeignKey) -> bool:
    return fk.referenced_schema == "auth" and fk.referenced_table == "users"


def _resolve_ref(
    ref: RowRef,
    fk: ForeignKey | None,
    refs: Mapping[str, Any],
    named: Mapping[str, tuple[str, int]],
    qualified_column: str,
    where: str,
) -> Any:
    if ref.name not in refs:
        msg = f"{where}: {qualified_column} references row {ref.name!r}, which is not declared."
        raise SqlProofScenarioError(msg)
    if fk is not None:
        target = named[ref.name][0]
        target_schema, target_name = target.split(".", 1)
        if fk.referenced_table != target_name or fk.referenced_schema not in (None, target_schema):
            expected = (
                f"{fk.referenced_schema}.{fk.referenced_table}"
                if fk.referenced_schema
                else fk.referenced_table
            )
            msg = (
                f"{where}: {qualified_column} references {expected}, but "
                f"{ref!r} is a row of {target}."
            )
            raise SqlProofScenarioError(msg)
    return refs[ref.name]


def _check_literal(table: Table, column: Column, value: Any, where: str) -> None:
    qualified_column = f"{table.qualified_name}.{column.name}"
    if value is None:
        if not column.nullable:
            msg = f"{where}: {qualified_column} is NOT NULL and cannot be pinned to None."
            raise SqlProofScenarioError(msg)
        return
    pg_type = column.type
    if pg_type.kind == "enum" and str(value) not in pg_type.enum_values:
        allowed = ", ".join(pg_type.enum_values)
        msg = f"{where}: {value!r} is not a value of {qualified_column}'s enum ({allowed})."
        raise SqlProofScenarioError(msg)
    for check in table.check_constraints:
        parsed = check.parsed or parse_check_expression(check.expression)
        if parsed is not None and _violates(parsed, column.name, value):
            msg = f"{where}: {value!r} violates CHECK ({check.expression}) on {qualified_column}."
            raise SqlProofScenarioError(msg)


def _violates(check: ParsedCheck, column: str, value: Any) -> bool:
    if check.kind == "compound":
        atoms: tuple[ParsedCheck, ...] = check.payload
        return any(_violates(atom, column, value) for atom in atoms)
    if check.column != column:
        return False
    if check.kind == "range":
        op, bound = check.payload
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return False
        return not {
            ">=": number >= bound,
            ">": number > bound,
            "<=": number <= bound,
            "<": number < bound,
        }[op]
    if check.kind == "in_set":
        return not _member(value, check.payload)
    if check.kind == "not_in":
        return _member(value, check.payload)
    if check.kind in ("length", "cardinality"):
        op, length = check.payload
        if check.kind == "cardinality":
            # An empty array may pass (`array_length(v, 1)` is NULL for it),
            # so only a non-empty pinned list is checked; Postgres decides
            # the rest.
            if not isinstance(value, list) or not value:
                return False
            size = len(cast(list[Any], value))  # type: ignore[redundant-cast]
        else:
            size = len(str(value))
        return not {
            ">=": size >= length,
            ">": size > length,
            "<=": size <= length,
            "<": size < length,
            "=": size == length,
        }[op]
    return False


def _member(value: Any, options: Sequence[Any]) -> bool:
    for option in options:
        if option == value or str(option) == str(value):
            return True
        try:
            if Decimal(str(option)) == Decimal(str(value)):
                return True
        except (InvalidOperation, ValueError):
            continue
    return False


def _check_unique_collisions(
    layout: _Layout, column: Column, pinned: Sequence[Any], where: str
) -> None:
    table = layout.table
    assert table is not None
    if not _is_single_column_unique(table, column.name):
        return
    qualified_column = f"{layout.qualified}.{column.name}"
    seen: dict[Any, int] = {}
    for group, value in zip(layout.groups, pinned, strict=True):
        if value is _UNSET or callable(value):
            continue
        if isinstance(value, list | dict):
            value = json.dumps(value, sort_keys=True, default=str)
        seen[value] = seen.get(value, 0) + group.count
        if seen[value] > 1:
            msg = (
                f"{where}: {qualified_column} is UNIQUE but {value!r} is pinned on "
                f"{seen[value]} rows."
            )
            raise SqlProofScenarioError(msg)
    if column.default is not None or _foreign_key_for_column(table, column.name) is not None:
        return
    generated = {
        _comparable(_unique_value(column.name, column.type, index))
        for group_index, (group, value) in enumerate(zip(layout.groups, pinned, strict=True))
        if value is _UNSET
        for index in layout.indexes(group_index)
        if group.count
    }
    clashes = sorted(str(value) for value in seen if value in generated)
    if clashes:
        msg = (
            f"{where}: {qualified_column} is UNIQUE and the generator assigns "
            f"{', '.join(clashes)} to other rows; pin a different value."
        )
        raise SqlProofScenarioError(msg)


def _comparable(value: Any) -> Any:
    """A generated UNIQUE value in the form pinned ones are compared in: an
    array column's list as its JSON text, as `_check_unique_collisions`
    writes a pinned list."""
    if isinstance(value, list):
        return json.dumps(value, sort_keys=True, default=_plain_json)
    return value


def _plain_json(value: Any) -> Any:
    return value.obj if isinstance(value, Json) else str(value)


def _looks_volatile(default: str) -> bool:
    return bool(
        _CLOCK_DEFAULT.search(default)
        or _UUID_DEFAULT.search(default)
        or _RANDOM_DEFAULT.search(default)
    )


def _volatile_default(
    column: Column, scenario_name: str, seed: int, qualified: str, clock: datetime
) -> Callable[[int], Any] | None:
    """A deterministic stand-in for a volatile column default, or None.

    `now()`-style defaults become the scenario clock, as every row loaded
    in one transaction would see a single `now()`; `gen_random_uuid()`
    becomes a UUID derived from the seed and row index; `random()` a float
    derived the same way. Anything else -- sequences included, which the
    loader resets -- is left to Postgres."""
    default = column.default
    if default is None:
        return None
    type_name = _base_type_name(column)
    if _CLOCK_DEFAULT.search(default):
        if type_name in _TIMESTAMPTZ:
            return lambda _index: clock
        if type_name in _TIMESTAMP:
            naive = clock.replace(tzinfo=None)
            return lambda _index: naive
        if type_name == "date":
            day = clock.date()
            return lambda _index: day
        return None
    if _UUID_DEFAULT.search(default) and type_name == "uuid":
        name = f"{qualified}.{column.name}"
        return lambda index: _uuid(scenario_name, seed, name, index)
    if _RANDOM_DEFAULT.search(default) and type_name in _FLOAT:
        name = f"{qualified}.{column.name}"
        return lambda index: _unit(scenario_name, seed, name, index)
    return None
