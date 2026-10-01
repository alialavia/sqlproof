"""`seed_test_users_directly` / `supabase_proof` against a stubbed auth schema (#115).

Plain-Postgres projects often stand in for Supabase's auth schema with a
minimal stub such as::

    create schema auth;
    create table auth.users (id uuid primary key, email text not null unique);

The shared test database already carries the full Supabase `auth`
schema, so this module creates a throwaway database (unique name,
dropped at the end) to hold the stub. Requires CREATEDB rights, like
`test_mutation_live.py`.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from uuid import uuid4

import psycopg
import pytest
from psycopg import conninfo, sql
from psycopg.rows import dict_row

from sqlproof.client import PsycopgSqlProofClient
from sqlproof.contrib.supabase import seed_test_users_directly
from sqlproof.exceptions import SqlProofSchemaError

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

STUB_SCHEMA_SQL = """
CREATE SCHEMA auth;
CREATE TABLE auth.users (id uuid PRIMARY KEY, email text NOT NULL UNIQUE);
CREATE TABLE public.profiles (
    id serial PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth.users (id),
    display_name text NOT NULL
);
"""


@pytest.fixture(scope="module")
def stub_dsn() -> Generator[str]:
    base_dsn = os.environ[DSN_ENV]
    db_name = f"sqlproof_stub_auth_{uuid4().hex[:12]}"
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    try:
        parts = conninfo.conninfo_to_dict(base_dsn)
        parts["dbname"] = db_name
        dsn = conninfo.make_conninfo(**parts)
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(STUB_SCHEMA_SQL)
        yield dsn
    finally:
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db_name))
            )


def test_seed_test_users_directly_works_on_stubbed_auth_users(stub_dsn: str) -> None:
    with psycopg.connect(
        stub_dsn,
        autocommit=True,
        row_factory=dict_row,  # pyright: ignore[reportArgumentType]
    ) as conn:
        client = PsycopgSqlProofClient(conn)
        first = seed_test_users_directly(client, count=3, email_prefix="stubtest_")
        second = seed_test_users_directly(client, count=3, email_prefix="stubtest_")
        emails = [
            row["email"]
            for row in client.query(
                "SELECT email FROM auth.users WHERE email LIKE 'stubtest%%' ORDER BY email"
            )
        ]

    assert len(first) == 3
    assert first == second
    assert emails == [
        "stubtest_0@test.invalid",
        "stubtest_1@test.invalid",
        "stubtest_2@test.invalid",
    ]


def test_seed_test_users_directly_names_unfillable_not_null_columns(stub_dsn: str) -> None:
    with psycopg.connect(stub_dsn, row_factory=dict_row) as conn:  # pyright: ignore[reportArgumentType]
        # Transaction rolled back on exit, so the extra column doesn't leak.
        # Backfill existing rows via a default, then drop it so the column
        # is NOT NULL with no default.
        conn.execute("ALTER TABLE auth.users ADD COLUMN tenant text NOT NULL DEFAULT 'x'")
        conn.execute("ALTER TABLE auth.users ALTER COLUMN tenant DROP DEFAULT")
        client = PsycopgSqlProofClient(conn)
        with pytest.raises(SqlProofSchemaError, match="tenant"):
            seed_test_users_directly(client, count=1)
        conn.rollback()


def test_supabase_proof_fixture_works_on_stubbed_auth_users(
    stub_dsn: str, pytester: pytest.Pytester
) -> None:
    pytester.makepyfile(
        """
        from hypothesis import HealthCheck, given, settings
        from hypothesis import strategies as st


        @settings(
            max_examples=5,
            deadline=None,
            suppress_health_check=[HealthCheck.function_scoped_fixture],
        )
        @given(data=st.data())
        def test_profiles_reference_seeded_users(supabase_proof, data):
            dataset = data.draw(supabase_proof.dataset_strategy(sizes={"profiles": 2}))
            with supabase_proof.client_for_dataset(dataset) as db:
                seeded = {
                    row["id"]
                    for row in db.query(
                        "SELECT id::text AS id FROM auth.users "
                        "WHERE email LIKE 'sqlproof%%@test.invalid'"
                    )
                }
                assert len(seeded) == 5
                rows = db.query("SELECT user_id::text AS user_id FROM public.profiles")
                assert len(rows) == 2
                assert {row["user_id"] for row in rows} <= seeded
        """
    )
    result = pytester.runpytest_subprocess(
        f"--sqlproof-database-url={stub_dsn}",
        "-p",
        "no:cacheprovider",
        "-v",
    )
    result.assert_outcomes(passed=1)
