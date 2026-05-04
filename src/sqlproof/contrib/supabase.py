from __future__ import annotations

import json
import os
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from importlib import import_module
from typing import Any, cast

from sqlproof.client import SqlProofClient

CLAIMS_GUC = "request.jwt.claims"


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
    """
    if count < 0:
        msg = "count must be non-negative."
        raise ValueError(msg)

    for index in range(count):
        email = f"{email_prefix}{index}@{email_domain}"
        db.execute(
            """
            INSERT INTO auth.users (id, aud, role, email)
            SELECT gen_random_uuid(), 'authenticated', 'authenticated', %s
            WHERE NOT EXISTS (SELECT 1 FROM auth.users WHERE email = %s)
            """,
            email,
            email,
        )

    rows = db.query(
        r"""
        SELECT id::text AS id
        FROM auth.users
        WHERE email LIKE %s ESCAPE '\'
        ORDER BY email
        """,
        f"{email_prefix.replace('_', r'\_')}%@{email_domain}",
    )
    return [row["id"] for row in rows]


def _email(user: object) -> str:
    if isinstance(user, Mapping):
        user_mapping = cast(Mapping[str, object], user)
        value = user_mapping.get("email")
        if isinstance(value, str):
            return value
    return ""
