"""End-to-end checks that the CI Postgres exposes the Supabase auth surface.

These tests don't exercise SqlProof itself; they assert that the
underlying database has the pieces a Supabase project relies on:

  * the `auth` schema with `auth.uid()` / `auth.role()` SQL helpers,
  * the `request.jwt.claims` GUC pattern that RLS policies use to
    identify the "logged-in" user during a test.

If these pass in CI we know the new `supabase/postgres` service
container is actually reachable and shaped the way the project's
Supabase contrib (and the `examples/supabase_rls/` example) expect.

Gated on `SQLPROOF_TEST_DATABASE_URL` like the rest of
`tests/integration/`. Skips locally when no DSN is set.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)


def test_auth_schema_exposes_uid_and_role_helpers() -> None:
    dsn = os.environ[DSN_ENV]
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT proname
            FROM pg_proc
            WHERE pronamespace = 'auth'::regnamespace
              AND proname IN ('uid', 'role', 'email')
            ORDER BY proname
            """
        )
        helpers = {row[0] for row in cur.fetchall()}
    assert helpers == {"email", "role", "uid"}, (
        f"expected auth.uid/role/email on a Supabase Postgres; got {helpers}"
    )


def test_auth_uid_returns_configured_jwt_sub_claim() -> None:
    """The standard RLS test pattern: set the JWT sub claim via the
    `request.jwt.claim.sub` GUC, then `auth.uid()` should return it.

    This image's `auth.uid()` is defined as
    `nullif(current_setting('request.jwt.claim.sub', true), '')::uuid`
    — so we set the singular `request.jwt.claim.sub` directly. Real
    Supabase RLS sets this from a verified JWT; in tests we stamp it
    ourselves.
    """
    dsn = os.environ[DSN_ENV]
    user_id = str(uuid4())

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("BEGIN")
        # `SET LOCAL` doesn't accept bind parameters; `set_config(name,
        # value, is_local=true)` is the parameterized equivalent.
        cur.execute("SELECT set_config('request.jwt.claim.sub', %s, true)", (user_id,))
        cur.execute("SELECT auth.uid()::text")
        seen = cur.fetchone()
        cur.execute("ROLLBACK")

    assert seen is not None
    assert seen[0] == user_id


def test_plpgsql_check_extension_is_installed() -> None:
    """The CI workflow runs CREATE EXTENSION plpgsql_check before pytest;
    this test asserts that step actually took effect."""
    dsn = os.environ[DSN_ENV]
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'plpgsql_check'")
        assert cur.fetchone() is not None, (
            "plpgsql_check should be pre-installed by the CI setup step"
        )
