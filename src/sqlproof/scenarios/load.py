"""Load a compiled scenario into a real database, atomically.

The load runs in one transaction: either every table holds the
scenario's rows afterwards, or nothing changed. Like the scale sweep, it
refuses to touch modelled tables that already hold rows -- including
rows row-level security hides from this role -- unless the caller opts
in with `truncate_existing=True`.

What makes two loads of the same scenario identical, beyond the bulk
generator's seeded values: volatile defaults are replaced at compile
time (see `compile._volatile_default`), sequences owned by modelled
tables are reset before the load, and `auth.users` rows get ids derived
from the scenario name, seed and row index.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlproof import _database as database
from sqlproof._database import dict_row, tuple_row
from sqlproof._version import __version__
from sqlproof.exceptions import SqlProofScenarioError
from sqlproof.scale.load import analyze, load_dataset, row_counts, truncate
from sqlproof.schema.introspect import introspect_schema

if TYPE_CHECKING:
    from sqlproof.scenarios.compile import AuthUser
    from sqlproof.scenarios.model import Scenario
    from sqlproof.schema.model import SchemaInfo, Table

_AUTH_USERS_VALUES = {
    "id": "%s",
    "aud": "'authenticated'",
    "role": "'authenticated'",
    "email": "%s",
}


@dataclass(frozen=True, slots=True)
class LoadReport:
    """What a scenario load did, and what identifies it."""

    scenario: str
    seed: int
    rows: dict[str, int]
    auth_users: int
    refs: dict[str, Any]
    fingerprint: str
    schema_fingerprint: str
    sqlproof_version: str
    nondeterministic_defaults: tuple[str, ...]


def load_scenario(
    scenario: Scenario,
    dsn: str,
    *,
    schema: str = "public",
    truncate_existing: bool = False,
) -> LoadReport:
    """Introspect `schema` at `dsn`, compile `scenario` against it and load it.

    DESTRUCTIVE only with `truncate_existing=True`, which empties every
    modelled table in `schema` first. Without it, a modelled table that
    already holds rows makes the load refuse before writing anything.
    Tables the scenario does not mention are left empty either way.
    """
    with database.connect(dsn, row_factory=dict_row) as conn:
        schema_info = introspect_schema(conn, schema=schema)
        conn.commit()
        compiled = scenario.compile(schema_info)
        if not truncate_existing:
            _refuse_non_empty(conn, schema_info, compiled.name)
        with conn.transaction():
            if truncate_existing:
                truncate(conn, schema_info)
            sequences = _owned_sequences(conn, schema_info)
            for owned in sequences:
                conn.execute("SELECT setval(%s::regclass, 1, false)", [owned.sequence]).close()
            _insert_auth_users(conn, compiled.auth_users)
            loaded = load_dataset(
                conn, schema_info, compiled.sizes, seed=compiled.seed, columns=compiled.columns
            )
            # Leave each sequence past the rows just loaded, so the app's own
            # inserts afterwards do not collide with generated keys.
            for owned in sequences:
                conn.execute(
                    "SELECT setval(%s::regclass, "
                    f"coalesce(max({_quote(owned.column)}), 0) + 1, false) "
                    f"FROM {_quote(owned.table.schema)}.{_quote(owned.table.name)}",
                    [owned.sequence],
                ).close()
            analyze(conn, schema_info)
    by_name = {t.name: t.qualified_name for t in schema_info.tables}
    return LoadReport(
        scenario=compiled.name,
        seed=compiled.seed,
        rows={by_name.get(name, name): count for name, count in loaded.items()},
        auth_users=len(compiled.auth_users),
        refs=dict(compiled.refs),
        fingerprint=compiled.fingerprint,
        schema_fingerprint=compiled.schema_fingerprint,
        sqlproof_version=__version__,
        nondeterministic_defaults=compiled.nondeterministic_defaults,
    )


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _refuse_non_empty(conn: database.Connection, schema: SchemaInfo, name: str) -> None:
    counts = row_counts(conn, schema)
    occupied: list[str] = []
    for table in schema.tables:
        count = counts.get(table.name, 0)
        if count is None:
            occupied.append(
                f"{table.qualified_name} (row-level security hides its rows from this role)"
            )
        elif count > 0:
            occupied.append(f"{table.qualified_name} ({count} rows)")
    if occupied:
        msg = (
            f"scenario {name!r} refused to load: these tables are not known to be "
            f"empty: {', '.join(occupied)}. Nothing was changed. Load into an empty "
            "database, or pass truncate_existing=True (--truncate-existing) to "
            "delete their rows first."
        )
        raise SqlProofScenarioError(msg)


@dataclass(frozen=True, slots=True)
class _OwnedSequence:
    sequence: str
    table: Table
    column: str


def _owned_sequences(conn: database.Connection, schema: SchemaInfo) -> list[_OwnedSequence]:
    """Every sequence owned by a modelled column: serial and identity columns."""
    found: list[_OwnedSequence] = []
    with conn.cursor(row_factory=tuple_row) as cur:
        for table in schema.tables:
            qualified = f"{_quote(table.schema)}.{_quote(table.name)}"
            for column in table.columns:
                row: tuple[Any, ...] | None = cur.execute(
                    "SELECT pg_get_serial_sequence(%s, %s)", [qualified, column.name]
                ).fetchone()
                if row is not None and row[0]:
                    found.append(_OwnedSequence(str(row[0]), table, column.name))
    return found


def _insert_auth_users(conn: database.Connection, users: tuple[AuthUser, ...]) -> list[str]:
    """Insert the scenario's auth users; return the ids actually inserted
    (an id already present is left alone)."""
    if not users:
        return []
    with conn.cursor(row_factory=tuple_row) as cur:
        rows: list[tuple[Any, ...]] = cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'auth' AND table_name = 'users'"
        ).fetchall()
    present = {str(row[0]) for row in rows}
    if not {"id", "email"} <= present:
        msg = (
            "This scenario seeds auth.users rows, but auth.users is missing or lacks "
            "id/email columns. Load it into a Supabase database (or one with the "
            "Supabase auth schema applied)."
        )
        raise SqlProofScenarioError(msg)
    columns = [column for column in _AUTH_USERS_VALUES if column in present]
    sql = (
        f"INSERT INTO auth.users ({', '.join(columns)}) "
        f"VALUES ({', '.join(_AUTH_USERS_VALUES[c] for c in columns)}) "
        "ON CONFLICT DO NOTHING RETURNING id"
    )
    inserted: list[str] = []
    for user in users:
        params = [user.id if c == "id" else user.email for c in columns if c in ("id", "email")]
        with conn.cursor(row_factory=tuple_row) as cur:
            inserted.extend(str(row[0]) for row in cur.execute(sql, params).fetchall())
    return inserted
