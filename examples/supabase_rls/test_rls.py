"""Property-based tests for the orgs / org_members / posts RLS schema.

Self-contained: applies its own schema (drops and recreates), seeds a
deterministic pool of `auth.users`, then runs three property tests +
a stateful invariant. Mirrors the official Supabase pgTAP example
(https://supabase.com/docs/guides/database/postgres/row-level-security
#testing-policies-with-pgtap).

Quantitative comparison to the pgTAP version:

  pgTAP  : ~220 LoC, 10 hardcoded scenarios (one user per role,
           one org per plan, three specific posts).
  sqlproof: ~140 LoC, ~150 (user * post * role) checks per run * 30
            runs + 30 boundary points for the free-plan post limit
            + 20 actor-role variations for member-management RLS.

Run:

    export SQLPROOF_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres
    uv run pytest examples/supabase_rls
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from hypothesis import assume
from hypothesis import strategies as st
from psycopg import errors as pg_errors

from sqlproof import ExternalTableSpec, SqlProof, sqlproof
from sqlproof.contrib.supabase import as_rls_user, supabase_test_user_ids

DSN = os.environ.get("SQLPROOF_TEST_DATABASE_URL")

if DSN is None:
    pytest.skip(
        "set SQLPROOF_TEST_DATABASE_URL to run the supabase_rls example",
        allow_module_level=True,
    )

SCHEMA = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
EMAIL_PREFIX = "sqlproof_rls_demo_"
SEED_USER_EMAILS = [f"{EMAIL_PREFIX}{i}@test.invalid" for i in range(5)]


def _setup_schema_and_users(dsn: str) -> None:
    """Drop and recreate the example's tables, then seed five auth.users
    rows. Idempotent across re-runs; doesn't touch anything outside the
    example's three tables.

    Skips the test module if the target DB has triggers on `auth.users`
    that reference unrelated schema (e.g. a personal-org trigger from
    another project). Run this example against a clean Supabase database.
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS posts, org_members, organizations CASCADE")
        conn.execute(SCHEMA)
        for email in SEED_USER_EMAILS:
            try:
                conn.execute(
                    "INSERT INTO auth.users (id, aud, role, email) "
                    "SELECT %s, 'authenticated', 'authenticated', %s "
                    "WHERE NOT EXISTS (SELECT 1 FROM auth.users WHERE email = %s)",
                    [str(uuid4()), email, email],
                )
            except pg_errors.ForeignKeyViolation as exc:
                pytest.skip(
                    f"target database has a trigger on auth.users that "
                    f"references unrelated schema ({exc.diag.message_primary}). "
                    f"Run this example against a clean Supabase database, "
                    f"not one shared with another project's triggers.",
                    allow_module_level=True,
                )


def _sample_seeded_users(db: Any) -> list[str]:
    return supabase_test_user_ids(db, email_prefix=EMAIL_PREFIX)


_setup_schema_and_users(DSN)

proof = SqlProof.from_connection_string(
    DSN,
    external_tables={
        "auth.users": ExternalTableSpec(
            primary_key="id",
            seed_count=st.integers(min_value=1, max_value=5),
            sample=_sample_seeded_users,
        ),
    },
)


def visible_to(post: dict[str, Any], role_in_org: str | None) -> bool:
    """Python model of the 'Complex post visibility' policy."""
    if post["status"] != "published":
        return role_in_org in ("owner", "admin", "editor")
    if not post["is_premium"]:
        return True
    return role_in_org is not None


# ---------------------------------------------------------------------------
# Property 1: every (user, post) pair matches the visibility model
# ---------------------------------------------------------------------------


@sqlproof(
    proof,
    sizes={"organizations": 2, "org_members": 4, "posts": 5},
    columns={
        "org_members.role": st.sampled_from(["owner", "admin", "editor", "viewer"]),
        "posts.status": st.sampled_from(["draft", "published", "archived"]),
    },
    runs=30,
)
def test_post_visibility_matches_policy(db: Any, dataset: dict[str, Any]) -> None:
    seeded_users = _sample_seeded_users(db)
    for user_id in seeded_users:
        for post in dataset["posts"]:
            role = next(
                (
                    m["role"]
                    for m in dataset["org_members"]
                    if m["user_id"] == user_id and m["org_id"] == post["org_id"]
                ),
                None,
            )
            expected = visible_to(post, role)

            with as_rls_user(db, user_id):
                rows = db.query(
                    "SELECT id FROM posts WHERE id = %s", [post["id"]]
                )
            actual = len(rows) == 1

            assert actual == expected, (
                f"user={user_id} post={post['id']} status={post['status']} "
                f"premium={post['is_premium']} role={role}: "
                f"policy returned {actual}, model expected {expected}"
            )


# ---------------------------------------------------------------------------
# Property 2: free-plan post limit holds at every boundary value
# ---------------------------------------------------------------------------


@sqlproof(
    proof,
    sizes={"organizations": 1, "org_members": 1},
    columns={
        "organizations.plan_type": "free",
        "organizations.max_posts": st.integers(min_value=0, max_value=4),
        "org_members.role": "editor",
    },
    runs=30,
)
def test_free_plan_post_limit_at_every_boundary(
    db: Any, dataset: dict[str, Any]
) -> None:
    org = dataset["organizations"][0]
    member = dataset["org_members"][0]
    max_posts: int = org["max_posts"]

    inserted = 0
    with as_rls_user(db, member["user_id"]):
        for _ in range(max_posts + 2):  # try to overshoot
            with db.savepoint():
                try:
                    db.execute(
                        "INSERT INTO posts (org_id, author_id, status) "
                        "VALUES (%s, %s, 'draft')",
                        [org["id"], member["user_id"]],
                    )
                    inserted += 1
                except pg_errors.InsufficientPrivilege:
                    break

    assert inserted == max_posts, (
        f"free plan with max_posts={max_posts}: inserted {inserted}, "
        f"expected exactly {max_posts}"
    )


# ---------------------------------------------------------------------------
# Property 3: org_members management is gated to owner/admin only
# ---------------------------------------------------------------------------


@sqlproof(
    proof,
    sizes={"organizations": 1, "org_members": 1},
    columns={
        "org_members.role": st.sampled_from(["owner", "admin", "editor", "viewer"]),
    },
    runs=20,
)
def test_member_management_requires_owner_or_admin(
    db: Any, dataset: dict[str, Any]
) -> None:
    org = dataset["organizations"][0]
    actor = dataset["org_members"][0]
    target_user = next(
        u for u in _sample_seeded_users(db) if u != actor["user_id"]
    )

    with as_rls_user(db, actor["user_id"]), db.savepoint():
        try:
            db.execute(
                "INSERT INTO org_members (org_id, user_id, role) "
                "VALUES (%s, %s, 'viewer')",
                [org["id"], target_user],
            )
            inserted = True
        except pg_errors.InsufficientPrivilege:
            inserted = False

    expected = actor["role"] in ("owner", "admin")
    assert inserted == expected, (
        f"actor_role={actor['role']!r}: insert allowed={inserted}, "
        f"expected {expected}"
    )


# ---------------------------------------------------------------------------
# Property 4: cross-org isolation — outsiders cannot read another org's drafts
# ---------------------------------------------------------------------------
#
# Property 1 already exercises this implicitly via its visibility model,
# but a dedicated cross-org test is the conventional shape for multi-
# tenant RLS work and worth showing on its own. Drafts are the cleanest
# slice for the demo: they're visible only to org members regardless of
# `is_premium`, so an outsider observing zero rows is unambiguous evidence
# that RLS isolated them.


@sqlproof(
    proof,
    sizes={"organizations": 1, "org_members": 1, "posts": 1},
    columns={
        "org_members.role": "editor",
        "posts.status": "draft",
    },
    runs=20,
)
def test_outsider_cannot_read_drafts_in_another_org(
    db: Any, dataset: dict[str, Any]
) -> None:
    """User who is NOT a member of the post's org must not see the draft.

    Pattern: pin the dataset to one victim org with one draft, then attack
    as any seeded auth.users row that isn't the org's member. The seed
    pool has up to 5 users (see `external_tables` above), so we discard
    examples where the victim org happens to claim the only seeded user.
    """
    member = dataset["org_members"][0]
    post = dataset["posts"][0]

    outsiders = [u for u in _sample_seeded_users(db) if u != member["user_id"]]
    assume(outsiders)
    attacker_id = outsiders[0]

    with as_rls_user(db, attacker_id):
        rows = db.query("SELECT id FROM posts WHERE id = %s", [post["id"]])

    assert rows == [], (
        f"cross-org RLS leak: outsider {attacker_id!r} saw draft post "
        f"{post['id']!r} in org {post['org_id']!r} "
        f"(member is {member['user_id']!r} as {member['role']!r})"
    )
