"""Live-Postgres integration test for exclusion constraints (#3c).

Confirms that ``introspect_schema`` against ``pg_constraint`` /
``pg_operator`` / ``pg_am`` produces the same ExclusionConstraint
shape the parser emits, with column/operator pairs in the correct
order.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from sqlproof.schema.introspect import introspect_schema
from sqlproof.schema.model import ExclusionConstraint
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE TABLE bookings (
    id serial PRIMARY KEY,
    room integer NOT NULL,
    during tsrange NOT NULL,
    EXCLUDE USING gist (room WITH =, during WITH &&)
);
"""


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_introspect_exclusion_constraint_round_trips_through_postgres() -> None:
    """The introspector reads the same (column, operator) shape the
    parser produces.

    Failure case: pg_constraint.conexclop ordering drift relative to
    conkey, or pg_am.amname returning something unexpected, would
    misalign the columns and operators. The integration test joins
    the same way the introspector does, so any mismatch surfaces
    here.
    """
    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    schema_name = f"sqlproof_excl_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            connection.execute(f'SET search_path TO "{schema_name}"')
            for statement in SCHEMA_SQL.strip().split(";"):
                if statement.strip():
                    connection.execute(statement)

            # Parser-side
            parsed = parse_schema_sql(SCHEMA_SQL).table("bookings")
            assert parsed.exclusion_constraints == (
                ExclusionConstraint(
                    columns_with_operators=(("room", "="), ("during", "&&")),
                    access_method="gist",
                ),
            )

            # Introspector-side
            with connection.cursor(row_factory=psycopg.rows.dict_row) as cur:
                introspected = introspect_schema(cur, schema=schema_name).table(
                    "bookings"
                )
            assert len(introspected.exclusion_constraints) == 1
            constraint = introspected.exclusion_constraints[0]
            assert constraint.columns_with_operators == (
                ("room", "="),
                ("during", "&&"),
            )
            assert constraint.access_method == "gist"
        finally:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
