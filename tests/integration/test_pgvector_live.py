"""Live-Postgres integration test for pgvector foundation.

Verifies the end-to-end path with the `vector` extension installed:
  (a) `introspect_schema` recovers the dimension from atttypmod.
  (b) The generator emits valid vector literals.
  (c) Generated rows round-trip through psycopg into Postgres.

Skips if:
  - SQLPROOF_TEST_DATABASE_URL is unset, OR
  - CREATE EXTENSION vector fails on the target server (pgvector not
    installed).
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from sqlproof.schema.introspect import introspect_schema
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA_SQL = """
CREATE TABLE embeddings (
    id serial PRIMARY KEY,
    embedding vector(8) NOT NULL
);
"""


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_vector_column_round_trips_through_postgres() -> None:
    from sqlproof import SqlProof
    from sqlproof.config import SqlProofConfig

    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    schema_name = f"sqlproof_pgvector_{uuid4().hex}"

    with psycopg.connect(dsn, autocommit=True) as connection:
        # Probe for pgvector availability. CREATE EXTENSION at the
        # database level requires superuser; skip cleanly when missing
        # rather than failing the suite on hosts without pgvector.
        try:
            connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except psycopg.Error as exc:
            pytest.skip(f"pgvector extension not available: {exc}")

        connection.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            connection.execute(f'SET search_path TO "{schema_name}"')
            for statement in SCHEMA_SQL.strip().split(";"):
                if statement.strip():
                    connection.execute(statement)

            # Parser-side
            parsed = parse_schema_sql(SCHEMA_SQL).table("embeddings")
            assert parsed.column("embedding").type.name == "vector"
            assert parsed.column("embedding").type.modifiers == (8,)

            # Introspector-side
            with connection.cursor(row_factory=psycopg.rows.dict_row) as cur:
                introspected = introspect_schema(cur, schema=schema_name).table(
                    "embeddings", schema=schema_name
                )
            embedding_type = introspected.column("embedding").type
            assert embedding_type.name == "vector"
            assert embedding_type.modifiers == (8,)

            # End-to-end: generate and insert via SqlProof.check.
            # If the wire format breaks anywhere in the chain, this
            # raises before the property body runs.
            proof = SqlProof.from_config(
                SqlProofConfig(connection_string=dsn, schema=schema_name)
            )

            def property_check(db) -> None:
                rows = db.query(
                    f'SELECT embedding FROM "{schema_name}".embeddings'
                )
                assert all(row["embedding"] is not None for row in rows)

            proof.check(
                "vector-typed columns round-trip cleanly",
                sizes={"embeddings": 4},
                property=property_check,
                runs=2,
            )
        finally:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
