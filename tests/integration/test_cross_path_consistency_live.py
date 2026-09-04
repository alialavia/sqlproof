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
from sqlproof.schema.model import Column, ForeignKey, PgType, SchemaInfo, Table
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
    # Measured: the composite-PK shape draws at ~1.15% (23/2000), and a
    # simulated run of this test at the old max_examples=20 missed it
    # entirely in 155/200 (77.5%) independent runs. 50 doesn't close that
    # gap on its own -- see test_composite_primary_key_junction_table_loads
    # below, which is what actually guarantees that shape runs every time
    # -- but it's cheap (~3.15s/100 examples measured, so ~1.5s here) and
    # buys incidental extra coverage on the other two widened axes (jsonb
    # draws at 25.9%, strict CHECK at 10.6%).
    max_examples=50,
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


def _composite_pk_junction_schema(schema_name: str) -> SchemaInfo:
    """Fixed junction-table shape: two parents, one child keyed on both FKs.

    `schemas()` can *draw* this shape but only draws it ~1.15% of the
    time (23/2000 measured) -- rare enough that a run of the committed
    Hypothesis test misses it entirely most of the time (155/200
    simulated runs at the old max_examples=20; raising max_examples
    doesn't fix this cheaply, since the miss rate is still ~10% even at
    200 examples and every example is a live-Postgres round trip). The
    mixed-radix key assignment this exercises
    (`composite_key_values` in generators/bulk.py) is the most
    intricate logic this whole plan added, and until this test existed
    it had only ever been checked against unit tests, never a real
    database. `test_composite_primary_key_junction_table_loads_deterministically`
    below builds this shape directly so it runs every time, independent
    of draw luck.
    """
    customers = Table(
        schema=schema_name,
        name="customers",
        columns=(Column("id", PgType("scalar", "bigint"), False, None, False),),
        primary_key=("id",),
        foreign_keys=(),
        unique_constraints=(),
        check_constraints=(),
    )
    products = Table(
        schema=schema_name,
        name="products",
        columns=(Column("id", PgType("scalar", "bigint"), False, None, False),),
        primary_key=("id",),
        foreign_keys=(),
        unique_constraints=(),
        check_constraints=(),
    )
    # The join table: composite PK over both FK columns, plus a jsonb
    # column so that path (the `Jsonb(None)` -> real SQL NULL fix) is
    # deterministically covered here too, not left to draw luck either.
    line_items = Table(
        schema=schema_name,
        name="line_items",
        columns=(
            Column("customer_id", PgType("scalar", "bigint"), False, None, False),
            Column("product_id", PgType("scalar", "bigint"), False, None, False),
            Column("meta", PgType("scalar", "jsonb"), True, None, False),
        ),
        primary_key=("customer_id", "product_id"),
        foreign_keys=(
            ForeignKey(
                columns=("customer_id",),
                referenced_table="customers",
                referenced_columns=("id",),
                on_delete="NO ACTION",
                on_update="NO ACTION",
            ),
            ForeignKey(
                columns=("product_id",),
                referenced_table="products",
                referenced_columns=("id",),
                on_delete="NO ACTION",
                on_update="NO ACTION",
            ),
        ),
        unique_constraints=(),
        check_constraints=(),
    )
    return SchemaInfo(tables=(customers, products, line_items))


def test_composite_primary_key_junction_table_loads_deterministically():
    """Deterministic counterpart to schemas()'s rarely-drawn composite-PK shape.

    Exists because the probabilistic differential test above misses
    this structural path most of the time by draw luck alone (see
    `_composite_pk_junction_schema`'s docstring for the measured
    numbers). This test is not about coverage breadth -- it is the one
    guarantee that `composite_key_values`'s mixed-radix key assignment
    is checked against a real Postgres on every run.
    """
    schema_name = "xpath_composite"
    schema = _composite_pk_junction_schema(schema_name)
    sizes = {"customers": 10, "products": 10, "line_items": 40}
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        conn.execute(f'CREATE SCHEMA "{schema_name}"')
        conn.execute(f'SET search_path TO "{schema_name}"')
        try:
            conn.execute(_ddl_for(schema))
            # Postgres is the oracle: a duplicate composite key or a
            # dangling FK raises here, inside COPY, before any
            # assertion below ever runs.
            counts = load_dataset(conn, schema, sizes, seed=11)
            assert counts == sizes

            actual = conn.execute(
                f'SELECT count(*) FROM "{schema_name}"."line_items"'
            ).fetchone()[0]
            assert actual == sizes["line_items"]

            # Belt-and-suspenders on top of the PRIMARY KEY constraint
            # itself: no two rows share a composite key.
            duplicate_keys = conn.execute(
                f'SELECT count(*) FROM ('
                f'  SELECT customer_id, product_id FROM "{schema_name}"."line_items" '
                f"  GROUP BY customer_id, product_id HAVING count(*) > 1"
                f") dupes"
            ).fetchone()[0]
            assert duplicate_keys == 0

            # Every FK half of the composite key resolves to a live parent.
            orphan_customers = conn.execute(
                f'SELECT count(*) FROM "{schema_name}"."line_items" li '
                f'LEFT JOIN "{schema_name}"."customers" c ON c.id = li.customer_id '
                f"WHERE c.id IS NULL"
            ).fetchone()[0]
            orphan_products = conn.execute(
                f'SELECT count(*) FROM "{schema_name}"."line_items" li '
                f'LEFT JOIN "{schema_name}"."products" p ON p.id = li.product_id '
                f"WHERE p.id IS NULL"
            ).fetchone()[0]
            assert orphan_customers == 0
            assert orphan_products == 0
        finally:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
