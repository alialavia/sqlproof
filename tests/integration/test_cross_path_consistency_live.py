"""Differential test: both generation paths must produce loadable data.

Note what is NOT asserted -- that the paths produce the same values.
The bulk path deliberately draws from a narrower alphabet and does not
chase edge cases. The contract is validity, type coverage and
statistical shape, never value equality.
"""
from __future__ import annotations

import os
from dataclasses import replace

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings

from sqlproof.scale.load import load_dataset
from sqlproof.schema.model import SchemaInfo
from sqlproof.testing import schemas

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)


def _retarget_schema(schema: SchemaInfo, name: str) -> SchemaInfo:
    """Rebind every generated table onto Postgres schema `name`.

    `sqlproof.testing.schemas()` (Task 10) hardcodes `Table.schema =
    "public"` -- it has no notion of the isolated schema a live test
    owns. `load_dataset` / `copy_table` always fully-qualify the
    `COPY` target with `table.schema` (see `scale/load.py`), while the
    DDL below creates tables *unqualified*, relying on `search_path`
    to land them in our owned schema. Left unretargeted, the DDL lands
    tables in e.g. `xpath.users` while `load_dataset` tries to `COPY`
    into `public.users`, which doesn't exist -- an `UndefinedTable`
    that has nothing to do with data validity. Every downstream
    consumer keys off `table.name`, never `table.schema`
    (`resolve_insertion_plan` matches FKs by name only;
    `_adapt_insert_value` never reads `.schema`), so rebinding every
    table's `.schema` here is safe.
    """
    return replace(schema, tables=tuple(replace(t, schema=name) for t in schema.tables))


def _ddl_for(schema: SchemaInfo) -> str:
    parts = []
    for table in schema.tables:
        cols = []
        for column in table.columns:
            null = "" if column.nullable else " NOT NULL"
            cols.append(f'"{column.name}" {column.type.name}{null}')
        cols.append(f'PRIMARY KEY ({", ".join(table.primary_key)})')
        for fk in table.foreign_keys:
            cols.append(
                f'FOREIGN KEY ({", ".join(fk.columns)}) REFERENCES '
                f'"{fk.referenced_table}" ({", ".join(fk.referenced_columns)})'
            )
        for check in table.check_constraints:
            cols.append(f"CHECK ({check.expression})")
        parts.append(f'CREATE TABLE "{table.name}" ({", ".join(cols)});')
    return "\n".join(parts)


@given(schema=schemas(max_tables=3, max_columns=4))
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=list(HealthCheck),
)
def test_bulk_path_produces_loadable_data_for_any_generated_schema(schema):
    schema = _retarget_schema(schema, "xpath")
    sizes = {t.name: 40 for t in schema.tables}
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS xpath CASCADE")
        conn.execute("CREATE SCHEMA xpath")
        conn.execute("SET search_path TO xpath")
        try:
            conn.execute(_ddl_for(schema))
            # Postgres is the oracle. A constraint violation raises here.
            counts = load_dataset(conn, schema, sizes, seed=7)
            assert counts
            for name, expected in sizes.items():
                actual = conn.execute(f'SELECT count(*) FROM xpath."{name}"').fetchone()[0]
                assert actual == expected
        finally:
            conn.execute("DROP SCHEMA IF EXISTS xpath CASCADE")
