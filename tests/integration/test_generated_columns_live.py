"""Live-Postgres integration test for stored generated columns (#3b).

Validates the end-to-end path that unit tests can't exercise: a
schema with ``GENERATED ALWAYS AS (...) STORED`` columns is parsed,
the row generator skips the generated column, and Postgres accepts
the INSERT and computes the value itself.

This catches a class of failure where the parser-side and
generator-side fixes are correct in isolation but something in the
INSERT-building path (``core.py``) still emits the generated column
into the column list.
"""

from __future__ import annotations

import os
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA_SQL = """
CREATE TABLE line_items (
    id serial PRIMARY KEY,
    qty integer NOT NULL,
    unit_price numeric(10, 2) NOT NULL,
    amount_total numeric(10, 2) GENERATED ALWAYS AS (qty * unit_price) STORED
);
"""


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_stored_generated_column_survives_full_dataset_insert() -> None:
    """The complete pipeline — parse_schema_sql → generator →
    SqlProof INSERT — must accept a schema with a generated
    column. Failure case: Postgres rejects the INSERT with
    ``cannot insert a non-DEFAULT value into column "amount_total"``,
    surfacing the parser-side gap.
    """
    from sqlproof import SqlProof
    from sqlproof.config import SqlProofConfig

    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    schema_name = f"sqlproof_gen_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            for statement in SCHEMA_SQL.strip().split(";"):
                if statement.strip():
                    connection.execute(
                        statement.replace(
                            "CREATE TABLE line_items",
                            f'CREATE TABLE "{schema_name}".line_items',
                        )
                    )

            # Verify the parser flag survives the SqlProof entry point.
            parsed = parse_schema_sql(SCHEMA_SQL).table("line_items")
            assert parsed.column("amount_total").is_generated is True

            proof = SqlProof.from_config(
                SqlProofConfig(connection_string=dsn, schema=schema_name)
            )

            # Property body that just reads back the inserted rows and
            # confirms Postgres computed the generated column. If the
            # INSERT failed, we wouldn't even reach this assertion.
            def property_check(db) -> None:
                rows = db.query("SELECT qty, unit_price, amount_total FROM line_items")
                for row in rows:
                    expected = Decimal(row["qty"]) * row["unit_price"]
                    assert row["amount_total"] == expected, (
                        f"Generated column mismatch: got {row['amount_total']}, "
                        f"expected {expected}"
                    )

            # check() doesn't raise unless the property fails.
            proof.check(
                "stored generated columns are computed by Postgres",
                sizes={"line_items": 3},
                property=property_check,
                runs=1,
            )
        finally:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
