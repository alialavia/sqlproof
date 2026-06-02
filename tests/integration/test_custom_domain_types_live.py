"""Live-Postgres integration test for custom domain types (#4a).

Confirms two things the unit tests can't:

  (a) The introspector pulls custom domains from ``pg_type`` (with
      ``typtype='d'``) joined to ``pg_constraint`` for CHECK
      expressions, and the canonical text from
      ``pg_get_constraintdef`` is generator-readable.
  (b) An end-to-end ``SqlProof.check()`` against a real schema
      with a domain-typed column survives — the generator
      produces values that Postgres accepts.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from sqlproof.schema.introspect import introspect_schema
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA_SQL = """
CREATE DOMAIN positive_int AS integer CHECK (VALUE > 0);
CREATE TABLE products (
    id serial PRIMARY KEY,
    qty positive_int NOT NULL
);
"""


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_domain_typed_column_round_trips_through_postgres() -> None:
    """Failure case: pg_get_constraintdef returns the CHECK in a
    form the generator's substitute-and-refine path can't read,
    so every generated qty fails the domain constraint and the
    INSERT batch is rejected by Postgres."""
    from sqlproof import SqlProof
    from sqlproof.config import SqlProofConfig

    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    schema_name = f"sqlproof_dom_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            connection.execute(f'SET search_path TO "{schema_name}"')
            for statement in SCHEMA_SQL.strip().split(";"):
                if statement.strip():
                    connection.execute(statement)

            # Parser-side
            parsed = parse_schema_sql(SCHEMA_SQL).table("products")
            assert parsed.column("qty").type.kind == "domain"

            # Introspector-side
            with connection.cursor(row_factory=psycopg.rows.dict_row) as cur:
                introspected = introspect_schema(cur, schema=schema_name).table(
                    "products", schema=schema_name
                )
            qty_type = introspected.column("qty").type
            assert qty_type.kind == "domain"
            assert qty_type.base is not None
            assert qty_type.base.name == "int4"  # pg_type.typname for integer

            # End-to-end: SqlProof.check() runs a property against
            # a generated dataset. If the generator emits qty<=0,
            # Postgres rejects the INSERT and check() raises before
            # ever calling the property body.
            proof = SqlProof.from_config(
                SqlProofConfig(connection_string=dsn, schema=schema_name)
            )

            def property_check(db) -> None:
                # Schema-qualified — db.query doesn't always inherit
                # the dataset's search_path across connection reuse.
                rows = db.query(f'SELECT qty FROM "{schema_name}".products')
                for row in rows:
                    assert row["qty"] > 0, f"Domain constraint violated: {row}"

            proof.check(
                "domain-typed columns satisfy their CHECK constraint",
                sizes={"products": 4},
                property=property_check,
                runs=2,
            )
        finally:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
