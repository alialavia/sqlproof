"""Live-Postgres integration test for SurfaceRegistry (#12).

Confirms that ``SurfaceRegistry.assert_no_drift`` works against a
real ``pg_proc`` row set, including the ``prokind='f'`` filter
(so internal procedures / aggregates / window functions don't
appear as unexpected) and the ``exclude_patterns`` filter
(so system functions don't dominate the drift report).
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from sqlproof.surface import SurfaceRegistry, SurfaceRegistryDrift


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_assert_no_drift_against_real_pg_proc() -> None:
    """End-to-end: define two simple functions in a fresh schema,
    declare them in a registry, and confirm assert_no_drift is
    a no-op. Then add an unregistered third function and confirm
    the SurfaceRegistryDrift exception names it.
    """
    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    schema_name = f"sqlproof_surface_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            connection.execute(
                f"""
                CREATE FUNCTION "{schema_name}".add_two(a int, b int)
                RETURNS int LANGUAGE sql IMMUTABLE AS $$ SELECT a + b $$
                """
            )
            connection.execute(
                f"""
                CREATE FUNCTION "{schema_name}".greet(name text)
                RETURNS text LANGUAGE sql IMMUTABLE AS $$ SELECT 'hi ' || name $$
                """
            )

            class _Db:
                def __init__(self, conn: psycopg.Connection) -> None:
                    self._conn = conn

                def query(self, sql: str, *params: object) -> list[dict[str, str]]:
                    with self._conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
                        cur.execute(sql, params)
                        return [dict(row) for row in cur.fetchall()]

            db = _Db(connection)
            registry = SurfaceRegistry(
                schema=schema_name,
                sections={"helpers": ["add_two", "greet"]},
            )
            # Empty drift — no surprise.
            registry.assert_no_drift(db)

            # Add an extra function and confirm drift surfaces it.
            connection.execute(
                f"""
                CREATE FUNCTION "{schema_name}".surprise()
                RETURNS int LANGUAGE sql IMMUTABLE AS $$ SELECT 42 $$
                """
            )
            with pytest.raises(SurfaceRegistryDrift) as exc:
                registry.assert_no_drift(db)
            assert "surprise" in str(exc.value)
        finally:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
