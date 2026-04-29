from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from sqlproof import SqlProof
from sqlproof.config import SqlProofConfig
from sqlproof.exceptions import SqlProofPropertyFailure


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_dsn_backed_property_runs_against_live_postgres() -> None:
    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    schema_name = f"sqlproof_it_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            connection.execute(
                f"""
                CREATE TABLE "{schema_name}".orders (
                  id INTEGER PRIMARY KEY,
                  total INTEGER NOT NULL CHECK (total >= 0)
                )
                """
            )
            proof = SqlProof.from_config(
                SqlProofConfig(connection_string=dsn, schema=schema_name)
            )

            def property_fails(db) -> None:
                raise AssertionError(db.scalar("SELECT id FROM orders"))

            with pytest.raises(SqlProofPropertyFailure):
                proof.check(
                    "orders exist",
                    sizes={"orders": 1},
                    property=property_fails,
                    runs=1,
                )
        finally:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
