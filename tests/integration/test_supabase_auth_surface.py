"""End-to-end checks that the CI Postgres exposes the Supabase auth surface.

These tests don't exercise SqlProof itself; they assert that the
underlying database has the pieces a Supabase project relies on:

  * the `auth` schema with `auth.uid()` / `auth.role()` / `auth.email()`
    / `auth.jwt()` SQL helpers,
  * the JSON-aware `auth.uid()` that managed Supabase gets via GoTrue's
    `20220224000811_update_auth_functions` migration — applied to our
    CI Postgres by the workflow setup step.

If these pass in CI we know the new `supabase/postgres` service
container is reachable and shaped the way managed Supabase looks, so
downstream RLS tests (`src/sqlproof/contrib/supabase.as_rls_user`, the
`examples/supabase_rls/` example) behave the same as in production.

Gated on `SQLPROOF_TEST_DATABASE_URL` like the rest of
`tests/integration/`. Skips locally when no DSN is set.
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import psycopg
import pytest

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)


def test_auth_schema_exposes_uid_role_email_jwt_helpers() -> None:
    dsn = os.environ[DSN_ENV]
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT proname
            FROM pg_proc
            WHERE pronamespace = 'auth'::regnamespace
              AND proname IN ('uid', 'role', 'email', 'jwt')
            ORDER BY proname
            """
        )
        helpers = {row[0] for row in cur.fetchall()}
    assert helpers == {"email", "jwt", "role", "uid"}, (
        "expected auth.uid/role/email/jwt after CI migration step; "
        f"got {helpers}. The workflow's 'Bring Postgres surface into "
        "lockstep with managed Supabase' step may have failed."
    )


def test_auth_uid_reads_singular_request_jwt_claim_sub() -> None:
    """Legacy singular GUC path — what the bare image's original
    `auth.uid()` supported and what `coalesce(...)` still falls back to.
    Important to keep working so anything writing the singular GUC
    (older PostgREST, direct SET LOCAL in tests) still resolves.
    """
    dsn = os.environ[DSN_ENV]
    user_id = str(uuid4())

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute(
            "SELECT set_config('request.jwt.claim.sub', %s, true)", (user_id,)
        )
        cur.execute("SELECT auth.uid()::text")
        seen = cur.fetchone()
        cur.execute("ROLLBACK")

    assert seen is not None
    assert seen[0] == user_id


def test_auth_uid_reads_json_request_jwt_claims() -> None:
    """JSON GUC path — what PostgREST 8+ sets and what
    `sqlproof.contrib.supabase.as_rls_user` writes. The CI workflow's
    migration step is what enables this on the bare image; without it,
    `auth.uid()` would return NULL here and every RLS-using test in
    `examples/supabase_rls/` would silently fail authentication.
    """
    dsn = os.environ[DSN_ENV]
    user_id = str(uuid4())
    claims = json.dumps({"sub": user_id, "role": "authenticated"})

    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SELECT set_config('request.jwt.claims', %s, true)", (claims,))
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
