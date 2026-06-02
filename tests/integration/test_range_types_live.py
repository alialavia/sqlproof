"""Live-Postgres integration test for range types (#4b).

Confirms that:
  (a) ``introspect_schema`` against a real ``pg_type`` row for
      a range column resolves to ``kind="range"`` with the
      element type as ``base``.
  (b) The generator's ``Range`` objects round-trip through
      psycopg's adapter into Postgres without rejection.

Without these, the unit tests' parser invariant could be correct
in isolation while the introspect side or the wire-format adapter
silently broke real-world usage.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from sqlproof.schema.introspect import introspect_schema
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA_SQL = """
CREATE TABLE bookings (
    id serial PRIMARY KEY,
    during tstzrange NOT NULL,
    quantity int4range NOT NULL
);
"""


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_range_typed_columns_round_trip_through_postgres() -> None:
    """Failure case: int4range / tstzrange columns generate as
    text fallback, and the INSERT batch errors with `invalid
    input syntax for type tstzrange` — surfacing the parser-side
    or introspector-side gap."""
    from sqlproof import SqlProof
    from sqlproof.config import SqlProofConfig

    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    schema_name = f"sqlproof_range_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            connection.execute(f'SET search_path TO "{schema_name}"')
            for statement in SCHEMA_SQL.strip().split(";"):
                if statement.strip():
                    connection.execute(statement)

            # Parser-side
            parsed = parse_schema_sql(SCHEMA_SQL).table("bookings")
            assert parsed.column("during").type.kind == "range"
            assert parsed.column("quantity").type.kind == "range"

            # Introspector-side
            with connection.cursor(row_factory=psycopg.rows.dict_row) as cur:
                introspected = introspect_schema(cur, schema=schema_name).table(
                    "bookings", schema=schema_name
                )
            during_type = introspected.column("during").type
            assert during_type.kind == "range"
            assert during_type.name == "tstzrange"

            # End-to-end: generate a dataset and let psycopg insert
            # the Range objects into Postgres. If anything in the
            # adapter chain breaks, check() raises before the property
            # body runs.
            proof = SqlProof.from_config(
                SqlProofConfig(connection_string=dsn, schema=schema_name)
            )

            def property_check(db) -> None:
                # Schema-qualified — db.query doesn't always inherit
                # the dataset's search_path across connection reuse.
                rows = db.query(
                    f'SELECT during, quantity FROM "{schema_name}".bookings'
                )
                assert all(row["during"] is not None for row in rows)
                assert all(row["quantity"] is not None for row in rows)

            proof.check(
                "range-typed columns round-trip cleanly",
                sizes={"bookings": 4},
                property=property_check,
                runs=2,
            )
        finally:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
