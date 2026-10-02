---
title: Testing Supabase Apps
description: Use the contrib helpers to test RLS policies and auth-driven behavior on a Supabase schema.
---

The `sqlproof.contrib.supabase` module bundles helpers for the parts of a
Supabase test setup that don't generalize to plain Postgres: auth-user
seeding and JWT-claim impersonation. These live in `contrib/` (not core)
because the JWT-claim shape and the `auth.users` table are
Supabase/PostgREST conventions, not Postgres features.

## Seeding test users

You have two paths depending on whether your test environment can reach
Supabase's admin API:

### Direct SQL insert (preferred locally)

When you're running against a local Supabase or any DB connection that has
write access to `auth.users`:

```python
from sqlproof.contrib.supabase import seed_test_users_directly

user_ids = seed_test_users_directly(db, count=5)
# Inserts users with emails sqlproof_0@test.invalid ... sqlproof_4@test.invalid
# Returns a list of user_ids matching the email pattern.
# Idempotent: re-running won't duplicate.
```

### Admin API (preferred in CI when service-role key is available)

When you have `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` set:

```python
from sqlproof.contrib.supabase import seed_supabase_test_users

seed_supabase_test_users(db=object(), count=5)  # `db` arg unused for admin API
```

Both helpers use the same email pattern, so a test that samples from
`auth.users WHERE email LIKE 'sqlproof_%@test.invalid'` works regardless of
which path was taken.

### Wiring into `ExternalTableSpec`

Once users exist, register `auth.users` as an external parent for FK
generation:

```python
from sqlproof import ExternalTableSpec, SqlProof
from hypothesis import strategies as st

def sample_test_user_ids(db) -> list[str]:
    rows = db.query(
        "SELECT id FROM auth.users WHERE email LIKE 'sqlproof_%%@test.invalid'"
    )
    return [row["id"] for row in rows]

proof = SqlProof.from_connection_string(
    "postgresql://...",
    external_tables={
        "auth.users": ExternalTableSpec(
            primary_key="id",
            seed_count=st.integers(min_value=1, max_value=5),
            sample=sample_test_user_ids,
        )
    },
)
```

Now any FK column referencing `auth.users(id)` in your generated dataset
draws from the seeded test users.

## Acting as a user (RLS testing)

`as_rls_user` is a context manager that runs a block as a Supabase user
**with RLS enforced**. It sets `request.jwt.claims` for the current
transaction so PostgREST/Supabase auth helpers (`auth.uid()`, `auth.jwt()`,
`auth.role()`) resolve to the given user, and runs
`SET LOCAL ROLE authenticated` so policies actually apply:

```python
from sqlproof.contrib.supabase import as_rls_user

with as_rls_user(db, user_id):
    # Inside the block, queries run as `authenticated` and RLS policies
    # that check auth.uid() see `user_id`.
    rows = db.query("SELECT * FROM projects")  # filtered by RLS
```

The role switch is the important part. The test connection is normally the
`postgres` superuser, which has `BYPASSRLS`: setting JWT claims alone
leaves every policy unevaluated, so "non-owner is blocked" tests fail on a
correct schema and "owner can read" tests pass even with RLS disabled. Pass
`role="anon"` (or another role) to test a different Postgres role.
`SET LOCAL` needs a transaction, which `client_for_dataset` and the
`supabase_db` fixture already provide.

### Claims only: `as_supabase_user`

`as_supabase_user(db, user_id)` sets the same JWT claims but does **not**
change role. Use it only when you need `auth.uid()` resolved without
enforcing policies — for example, asserting what a function returns for a
given caller. Don't use it to test RLS.

Important properties (both helpers):

- **Restores prior claim on exit.** Nested calls stack and unwind
  correctly; `as_rls_user` also runs `RESET ROLE` on exit.
- **Safe under exceptions.** Implemented with `try/finally`.
- **Composable with `db.savepoint()`** — wrap whichever you want to take
  precedence first.
- **Plain Postgres, no Supabase RPC.** The helper only sets a GUC; it
  doesn't talk to the auth API.

### Custom claims

Pass `extra_claims` to merge additional JWT fields:

```python
with as_rls_user(
    db, user_id,
    extra_claims={"app_metadata": {"plan": "pro"}},
):
    ...
```

Order: `{"sub": user_id, "role": role, **extra_claims}`. For `as_rls_user`,
`role=` sets both the `role` claim and the Postgres role it switches to
(default `"authenticated"`); prefer `role=` over a `role` key in
`extra_claims`, which would change only the claim.

## Stateful + RLS

The combo earns its keep on RLS regression tests, where bugs surface
across membership churn rather than a single permission check. See the
[stateful testing guide](/api/state-machine/) for an end-to-end example
covering `get_member_project_ids` / `get_editor_project_ids` against
`project_members` mutations.

## Caveats

- `seed_test_users_directly` requires the DB connection to have INSERT
  privilege on `auth.users`. Local Supabase grants this by default; managed
  Supabase typically does not — use the admin-API path there.
- Setting `request.jwt.claims` (what `as_supabase_user` does) only
  changes what `auth.uid()` returns for the transaction; it does not
  change the connection's Postgres role, so a superuser connection still
  bypasses RLS. Queries outside `as_rls_user` run as the connection's role
  (typically `postgres` or `service_role`).
- The `auth.users` schema can drift across Supabase versions. The helpers
  insert minimal columns (`id`, `aud`, `role`, `email`); if your test data
  needs richer auth metadata, insert directly with raw SQL.
