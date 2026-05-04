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

`as_supabase_user` is a context manager that sets `request.jwt.claims` for
the current transaction so PostgREST/Supabase auth helpers (`auth.uid()`,
`auth.jwt()`, `auth.role()`) resolve to the given user:

```python
from sqlproof.contrib.supabase import as_supabase_user

with as_supabase_user(db, user_id):
    # Inside the block, RLS policies that check auth.uid() see `user_id`.
    rows = db.query("SELECT * FROM projects")  # filtered by RLS
```

Important properties:

- **Restores prior claim on exit.** Nested `as_supabase_user` calls stack
  and unwind correctly.
- **Safe under exceptions.** Implemented with `try/finally`.
- **Composable with `db.savepoint()`** — wrap whichever you want to take
  precedence first.
- **Plain Postgres, no Supabase RPC.** The helper only sets a GUC; it
  doesn't talk to the auth API.

### Custom claims

Pass `extra_claims` to merge additional JWT fields:

```python
with as_supabase_user(
    db, user_id,
    role="service_role",  # or pass via extra_claims; explicit arg wins
    extra_claims={"app_metadata": {"plan": "pro"}},
):
    ...
```

Order: `{"sub": user_id, "role": role, **extra_claims}`. Pass `role` in
`extra_claims` to override the default `"authenticated"`.

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
- Setting `request.jwt.claims` only changes what `auth.uid()` returns for
  the transaction; it does not bypass RLS or change the connection's
  Postgres role. Assume your tests run as the connection's role
  (typically `postgres` or `service_role`) for non-RLS queries.
- The `auth.users` schema can drift across Supabase versions. The helpers
  insert minimal columns (`id`, `aud`, `role`, `email`); if your test data
  needs richer auth metadata, insert directly with raw SQL.
