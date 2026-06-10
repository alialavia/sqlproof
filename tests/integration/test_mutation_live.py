"""End-to-end mutation run against live Postgres.

Builds a throwaway template database (table + LANGUAGE sql function),
writes a minimal pytest suite to tmp_path, and runs two mutants:

  - WHERE-clause inversion -> the suite recomputes the sum in Python,
    so this mutant MUST be killed.
  - COALESCE fallback 0 -> 1 -> the suite never queries a user with
    zero rows, so this mutant MUST survive (the classic untested
    empty-group case).

Skips if SQLPROOF_TEST_DATABASE_URL is unset. Requires CREATEDB rights
on the target server (the CI supabase/postgres service has them).
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import conninfo, sql

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.model import MutationSet, Replace
from sqlproof.mutation.runner import run_mutation_tests

SCHEMA_SQL = """
CREATE TABLE usage_events (
    id serial PRIMARY KEY,
    user_id integer NOT NULL,
    amount integer NOT NULL CHECK (amount >= 0)
);

CREATE FUNCTION total_usage(p_user integer) RETURNS bigint
LANGUAGE sql STABLE
AS $$
    SELECT COALESCE(SUM(amount), 0) FROM usage_events WHERE user_id = p_user
$$;
"""

INNER_TEST = """
    from __future__ import annotations

    import os

    import psycopg


    def test_total_usage_matches_python_sum() -> None:
        dsn = os.environ["SQLPROOF_MUTATION_TEST_DSN"]
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("DELETE FROM usage_events")
            rows = [(1, 10), (1, 32), (2, 5)]
            for user_id, amount in rows:
                connection.execute(
                    "INSERT INTO usage_events (user_id, amount) VALUES (%s, %s)",
                    (user_id, amount),
                )
            cursor = connection.execute("SELECT total_usage(1)")
            assert cursor.fetchone()[0] == 42
"""


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_mutation_run_kills_and_survives(tmp_path: Path) -> None:
    base_dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    template_name = f"sqlproof_mut_tmpl_{uuid4().hex[:12]}"

    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(template_name))
        )
    try:
        parts = conninfo.conninfo_to_dict(base_dsn)
        parts["dbname"] = template_name
        template_dsn = conninfo.make_conninfo(**parts)
        with psycopg.connect(template_dsn, autocommit=True) as connection:
            connection.execute(SCHEMA_SQL)
        # The template must have zero connections during cloning -> the
        # context managers above are closed before run_mutation_tests.

        schema_file = tmp_path / "schema.sql"
        schema_file.write_text(SCHEMA_SQL, encoding="utf-8")
        test_file = tmp_path / "test_inner_billing.py"
        test_file.write_text(textwrap.dedent(INNER_TEST), encoding="utf-8")

        mutations = MutationSet.for_function(
            "total_usage",
            [
                Replace("user_id = p_user", "user_id <> p_user"),
                Replace("COALESCE(SUM(amount), 0)", "COALESCE(SUM(amount), 1)"),
            ],
        )
        result = run_mutation_tests(
            mutations,
            schema_file=schema_file,
            database_url=template_dsn,
            pytest_args=[str(test_file), "-q", "-p", "no:cacheprovider"],
            env_var="SQLPROOF_MUTATION_TEST_DSN",
        )

        statuses = {o.description: o.status for o in result.outcomes}
        assert statuses[
            "total_usage: replace 'user_id = p_user' -> 'user_id <> p_user'"
        ] == "killed", result.to_dict()
        assert statuses[
            "total_usage: replace 'COALESCE(SUM(amount), 0)' -> 'COALESCE(SUM(amount), 1)'"
        ] == "survived", result.to_dict()

        with pytest.raises(SqlProofMutationError, match="COALESCE"):
            result.assert_no_survivors()

        # No clone databases left behind by THIS run (scoped to its mutant
        # ids — a shared server may hold orphans from previously
        # interrupted runs, which are not this test's concern).
        expected_clones = [f"sqlproof_mutant_{o.mutant_id}" for o in result.outcomes]
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            cursor = admin.execute(
                "SELECT datname FROM pg_database WHERE datname = ANY(%s)",
                (expected_clones,),
            )
            assert cursor.fetchall() == []
    finally:
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(template_name)
                )
            )
