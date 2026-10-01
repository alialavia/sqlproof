from __future__ import annotations

import json
import os
import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from importlib import import_module
from typing import Any, cast

from sqlproof.client import SqlProofClient
from sqlproof.exceptions import SqlProofSchemaError

CLAIMS_GUC = "request.jwt.claims"

# `SET LOCAL ROLE <ident>` doesn't accept parameter substitution, so the
# caller's role kwarg is interpolated. Restrict to valid Postgres unquoted
# identifiers — letters, digits, underscores, leading non-digit — so a
# typo or unexpected input fails with a ValueError instead of a SQL
# injection vector.
_VALID_ROLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Columns `seed_test_users_directly` knows how to populate, in insert
# order, mapped to the SQL expression that fills them. Real Supabase
# `auth.users` has all four; stubbed auth schemas used in plain-Postgres
# test setups often carry only `id` and `email` (#115), so only the
# columns actually present are inserted.
_AUTH_USERS_SEED_EXPRESSIONS: dict[str, str] = {
    "id": "gen_random_uuid()",
    "aud": "'authenticated'",
    "role": "'authenticated'",
    "email": "%s",
}
# `id` is what FK sampling hands out; `email` is how seeded users are
# identified (idempotency check + `supabase_test_user_ids` lookup).
_AUTH_USERS_REQUIRED_COLUMNS = ("id", "email")


@contextmanager
def as_supabase_user(
    db: SqlProofClient,
    user_id: str,
    *,
    role: str = "authenticated",
    extra_claims: Mapping[str, Any] | None = None,
) -> Generator[None]:
    """Run a block as a Supabase auth user by setting `request.jwt.claims`.

    Sets the transaction-local `request.jwt.claims` GUC so that PostgREST/
    Supabase helpers (`auth.uid()`, `auth.jwt()`, `auth.role()`) resolve to
    the given user. The previous value of the GUC, if any, is restored on
    exit, so nested invocations stack correctly.

    Composable with `db.savepoint()`. Safe under exceptions.
    """
    prior = db.scalar("SELECT current_setting(%s, true)", CLAIMS_GUC)
    claims: dict[str, Any] = {"sub": user_id, "role": role}
    if extra_claims:
        claims.update(extra_claims)
    db.execute("SELECT set_config(%s, %s, true)", CLAIMS_GUC, json.dumps(claims))
    try:
        yield
    finally:
        restore_value = "" if prior in (None, "") else prior
        db.execute("SELECT set_config(%s, %s, true)", CLAIMS_GUC, restore_value)


@contextmanager
def as_rls_user(
    db: SqlProofClient,
    user_id: str,
    *,
    role: str = "authenticated",
    extra_claims: Mapping[str, Any] | None = None,
) -> Generator[None]:
    """Run a block as a Supabase auth user **with RLS actually enforced**.

    Combines two transaction-local switches:

      * ``as_supabase_user`` — sets ``request.jwt.claims`` so PostgREST/
        Supabase helpers (``auth.uid()``, ``auth.jwt()``, ``auth.role()``)
        resolve to ``user_id``.
      * ``SET LOCAL ROLE <role>`` — engages RLS. Without this, a
        connection running as the postgres superuser (the typical
        sqlproof DSN) bypasses RLS entirely (BYPASSRLS), so policies
        are never evaluated and tests pass for the wrong reason.
        ``RESET ROLE`` restores on exit.

    Use this whenever a test asserts RLS behaviour. For tests that only
    need ``auth.uid()`` resolved (no policy enforcement), ``as_supabase_user``
    on its own is enough — it doesn't change roles.

    Requires the caller to be in a transaction; ``SET LOCAL`` is a
    no-op outside one. Compose with ``db.savepoint()`` if the caller's
    test runner doesn't already wrap each example in a transaction.
    """
    if not _VALID_ROLE_RE.match(role):
        msg = f"role must be a valid Postgres identifier, got {role!r}"
        raise ValueError(msg)

    with as_supabase_user(db, user_id, role=role, extra_claims=extra_claims):
        db.execute(f"SET LOCAL ROLE {role}")
        try:
            yield
        finally:
            db.execute("RESET ROLE")


def supabase_test_user_ids(
    db: SqlProofClient,
    *,
    email_prefix: str = "sqlproof_",
    email_domain: str = "test.invalid",
) -> list[str]:
    """Return the IDs of Supabase test users matching the email pattern.

    The companion lookup to ``seed_test_users_directly``: the seed call
    creates the rows and returns their ids, but property-test sample
    callbacks need a way to re-discover those ids on every run without
    re-seeding. Use this as the ``sample`` for ``ExternalTableSpec``
    when ``auth.users`` rows persist across the session.
    """
    escaped_prefix = email_prefix.replace("_", r"\_")
    rows = db.query(
        r"""
        SELECT id::text AS id
        FROM auth.users
        WHERE email LIKE %s ESCAPE '\'
        ORDER BY email
        """,
        f"{escaped_prefix}%@{email_domain}",
    )
    return [row["id"] for row in rows]


def seed_supabase_test_users(
    db: SqlProofClient | object,
    count: int = 20,
    *,
    email_prefix: str = "sqlproof_",
    email_domain: str = "test.invalid",
    password: str = "test_password",
) -> None:
    """Create replaceable Supabase auth users for external table FK sampling."""
    del db
    if count < 0:
        msg = "count must be non-negative."
        raise ValueError(msg)

    httpx = import_module("httpx")
    service_role_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    with httpx.Client(
        base_url=os.environ["SUPABASE_URL"],
        headers={"Authorization": f"Bearer {service_role_key}", "apikey": service_role_key},
        timeout=5.0,
    ) as admin:
        response = admin.get("/auth/v1/admin/users")
        response.raise_for_status()
        existing = response.json()
        existing_emails = {_email(user) for user in existing.get("users", [])}

        for index in range(count):
            email = f"{email_prefix}{index}@{email_domain}"
            if email in existing_emails:
                continue
            create_response = admin.post(
                "/auth/v1/admin/users",
                json={
                    "email": email,
                    "password": password,
                    "email_confirm": True,
                },
            )
            create_response.raise_for_status()


def seed_test_users_directly(
    db: SqlProofClient,
    count: int = 20,
    *,
    email_prefix: str = "sqlproof_",
    email_domain: str = "test.invalid",
) -> list[str]:
    """Insert skeleton `auth.users` rows directly via SQL.

    Returns the user_ids of all sqlproof test users (newly inserted plus any
    pre-existing ones matching the email pattern). Idempotent: existing
    emails are preserved via `ON CONFLICT (email) DO NOTHING`.

    Use when the Supabase admin API is unavailable but the connection has
    write access to `auth.users` (e.g. local Supabase).

    Works against both the full Supabase `auth.users` table and minimal
    stubs (e.g. ``auth.users (id uuid primary key, email text not null
    unique)``): the table's columns are introspected and only those of
    ``id``, ``aud``, ``role`` and ``email`` that exist are populated.
    Raises ``SqlProofSchemaError`` when ``auth.users`` is missing, lacks
    ``id`` / ``email``, or has a NOT NULL column without a default that
    this helper can't fill.
    """
    if count < 0:
        msg = "count must be non-negative."
        raise ValueError(msg)

    columns = _auth_users_seed_columns(db)
    column_list = ", ".join(columns)
    value_list = ", ".join(_AUTH_USERS_SEED_EXPRESSIONS[column] for column in columns)
    insert_sql = f"""
        INSERT INTO auth.users ({column_list})
        SELECT {value_list}
        WHERE NOT EXISTS (SELECT 1 FROM auth.users WHERE email = %s)
        """

    for index in range(count):
        email = f"{email_prefix}{index}@{email_domain}"
        db.execute(insert_sql, email, email)

    return supabase_test_user_ids(
        db, email_prefix=email_prefix, email_domain=email_domain
    )


def _auth_users_seed_columns(db: SqlProofClient) -> list[str]:
    """Return the seedable `auth.users` columns present, in insert order.

    Raises ``SqlProofSchemaError`` if the table's shape can't be seeded.
    """
    rows = db.query(
        """
        SELECT column_name, is_nullable, column_default, is_identity, is_generated
        FROM information_schema.columns
        WHERE table_schema = 'auth' AND table_name = 'users'
        ORDER BY ordinal_position
        """
    )
    override_hint = (
        "To seed a differently shaped auth.users, override the `proof` (or "
        "`supabase_proof`) fixture in your conftest.py and register auth.users "
        "via ExternalTableSpec yourself; see the `proof` fixture docstring in "
        "sqlproof.pytest_plugin for an example."
    )
    if not rows:
        msg = (
            "seed_test_users_directly: table auth.users not found (or not visible "
            f"to the current role). {override_hint}"
        )
        raise SqlProofSchemaError(msg)

    present = {str(row["column_name"]) for row in rows}
    missing = [column for column in _AUTH_USERS_REQUIRED_COLUMNS if column not in present]
    if missing:
        msg = (
            "seed_test_users_directly: auth.users is missing required column(s) "
            f"{', '.join(missing)}; it needs at least "
            f"{', '.join(_AUTH_USERS_REQUIRED_COLUMNS)}. {override_hint}"
        )
        raise SqlProofSchemaError(msg)

    unfillable = [
        str(row["column_name"])
        for row in rows
        if row["column_name"] not in _AUTH_USERS_SEED_EXPRESSIONS
        and row["is_nullable"] == "NO"
        and row["column_default"] is None
        and row["is_identity"] != "YES"
        and row["is_generated"] != "ALWAYS"
    ]
    if unfillable:
        msg = (
            "seed_test_users_directly: auth.users has NOT NULL column(s) without a "
            f"default that sqlproof can't populate: {', '.join(unfillable)} "
            "(only id, aud, role and email are filled). Give those columns "
            f"defaults, or: {override_hint}"
        )
        raise SqlProofSchemaError(msg)

    return [column for column in _AUTH_USERS_SEED_EXPRESSIONS if column in present]


def _email(user: object) -> str:
    if isinstance(user, Mapping):
        user_mapping = cast(Mapping[str, object], user)
        value = user_mapping.get("email")
        if isinstance(value, str):
            return value
    return ""
