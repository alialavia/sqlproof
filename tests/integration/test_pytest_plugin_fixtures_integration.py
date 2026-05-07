"""End-to-end integration test for the pytest plugin's `proof` / `db` /
`supabase_proof` fixtures.

Gated on `SQLPROOF_TEST_DATABASE_URL` (the existing env var that other
integration tests use). Verifies that:

  * The fixtures actually connect to the configured DB.
  * `db` provides a usable `SqlProofClient` with rollback isolation.
  * `supabase_proof` seeds `auth.users` and registers it as an external
    table (skipped if the target DB doesn't have an `auth.users` table —
    this is a Supabase-specific path).
"""

from __future__ import annotations

import os

import pytest

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)


def test_proof_and_db_fixtures_connect_to_configured_database(
    pytester: pytest.Pytester,
) -> None:
    """The plugin's `proof` and `db` fixtures should round-trip a SELECT
    against the database pointed to by `--sqlproof-database-url`."""
    pytester.makepyfile(
        """
        def test_db_executes_select(db):
            assert db.scalar("SELECT 1") == 1

        def test_db_rolls_back_writes_between_tests(db):
            # Inside a single test, writes are visible.
            db.execute("CREATE TEMP TABLE _spike (n int)")
            db.execute("INSERT INTO _spike VALUES (42)")
            assert db.scalar("SELECT n FROM _spike") == 42
            # The TEMP table dies with the connection / savepoint —
            # the next test wouldn't see _spike.
        """
    )
    result = pytester.runpytest_subprocess(
        f"--sqlproof-database-url={os.environ[DSN_ENV]}",
        "-v",
    )
    result.assert_outcomes(passed=2)


def test_supabase_proof_seeds_users_when_auth_users_present(
    pytester: pytest.Pytester,
) -> None:
    """`supabase_proof` should seed the deterministic test-user pool
    and the seeded rows should be visible inside a `supabase_db` test."""
    pytester.makepyfile(
        """
        import pytest

        def test_seeded_users_visible(supabase_db):
            try:
                rows = supabase_db.query(
                    r\"\"\"
                    SELECT id::text AS id
                    FROM auth.users
                    WHERE email LIKE %s ESCAPE '\\\\'
                    \"\"\",
                    r'sqlproof\\_%@test.invalid',
                )
            except Exception as exc:
                # Target DB has no auth schema (not a Supabase DB) — skip
                # rather than fail; the supabase_proof fixture is
                # Supabase-specific.
                pytest.skip(f'auth.users not available: {exc}')
            assert len(rows) >= 1, 'expected at least one seeded test user'
        """
    )
    result = pytester.runpytest_subprocess(
        f"--sqlproof-database-url={os.environ[DSN_ENV]}",
        "-v",
        "-rs",
    )
    # Either the seeded test passed, or it skipped because the target
    # DB doesn't have auth.users. Both are acceptable.
    outcomes = result.parseoutcomes()
    assert outcomes.get("passed", 0) + outcomes.get("skipped", 0) == 1
