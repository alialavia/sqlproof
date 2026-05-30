---
title: Run SqlProof in your CI
description: Copy-paste GitHub Actions workflows for running SqlProof against vanilla Postgres or a Supabase-shaped database, including the auth-migration setup needed for RLS tests.
---

SqlProof is a regular Python library. To run it in your own repo's CI you
install it from PyPI, bring up a Postgres your tests can reach, point
SqlProof at it, and run pytest. This page gives you the copy-paste workflow
for the three shapes that come up in practice.

## Install

```bash
pip install --pre sqlproof
# or
uv add --prerelease=allow sqlproof
```

The `--pre` / `--prerelease=allow` flag is required while SqlProof is in
alpha (`0.1.0a1`). Once 1.0 ships, plain `pip install sqlproof` will work.

## How SqlProof finds your database

SqlProof's pytest plugin resolves the Postgres DSN in this order:

1. The `--sqlproof-database-url` pytest flag
2. The `SQLPROOF_DATABASE_URL` environment variable
3. The `SUPABASE_DB_URL` environment variable (so Supabase users can reuse
   the same env var their Supabase CLI sets)

If none of these is set, tests using the `proof` / `db` fixtures or the
`@sqlproof` decorator skip cleanly — they don't fail.

Set whichever is most convenient in your workflow's `env:` block. The
examples below use `SQLPROOF_DATABASE_URL`.

## Shape 1 — No database needed

If your tests only use the in-memory client or the pure data generator
(no `proof.check(...)` against a live DB), you don't need a Postgres at
all:

```yaml
# .github/workflows/test.yml
name: Tests
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install --pre sqlproof pytest
      - run: pytest
```

## Shape 2 — Vanilla Postgres

For projects that hit a real Postgres but don't use Supabase-specific
features (no `auth.uid()`, no RLS that depends on JWT claims):

```yaml
# .github/workflows/test.yml
name: Tests
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: postgres
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U postgres"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 10
    env:
      SQLPROOF_DATABASE_URL: postgresql://postgres:postgres@127.0.0.1:5432/postgres
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install --pre sqlproof pytest
      - run: pytest
```

That's it. The `services.postgres` block boots Postgres alongside your job,
the `env.SQLPROOF_DATABASE_URL` tells SqlProof where to find it, and pytest
picks up the `proof` / `db` fixtures automatically via the `pytest11` entry
point.

## Shape 3 — Supabase-shaped Postgres (with RLS / `auth.uid()`)

If your tests exercise RLS policies, `auth.uid()`, `auth.role()`, or
anything else that depends on Supabase's `auth` schema, this is the shape
you want.

**You'll need to do two things the vanilla shape doesn't:**

1. **Use the `supabase/postgres` image** instead of `postgres:16`. It ships
   the `auth` schema (with `auth.users`, `auth.uid()`, etc.), the
   `plpgsql_check` extension binary, `pgvector`, `pgsodium`, `pgjwt`, and
   everything else Supabase bundles.
2. **Apply the GoTrue auth migration** in a setup step. The bare image
   ships an outdated `auth.uid()` that only reads the legacy singular GUC
   `request.jwt.claim.sub`. PostgREST 8+ (and SqlProof's
   `as_rls_user` helper) write the modern JSON `request.jwt.claims` GUC.
   Managed Supabase patches this at deploy time via GoTrue migration
   [`20220224000811_update_auth_functions`](https://github.com/supabase/auth/blob/master/migrations/20220224000811_update_auth_functions.up.sql);
   we apply the same migration ourselves.

Without step 2, `auth.uid()` silently returns NULL in your tests and every
RLS policy evaluates as "not authenticated."

```yaml
# .github/workflows/test.yml
name: Tests
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        # Pin to a specific Supabase image tag. Check
        # https://hub.docker.com/r/supabase/postgres/tags for newer tags.
        image: supabase/postgres:15.8.1.040
        env:
          POSTGRES_PASSWORD: postgres
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U postgres -d postgres"
          --health-interval 10s
          --health-timeout 5s
          --health-retries 15
    env:
      SQLPROOF_DATABASE_URL: postgresql://postgres:postgres@127.0.0.1:5432/postgres
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Make Postgres match managed Supabase
        run: |
          # plpgsql_check ships with the image but isn't pre-installed
          # in the default database. Create it once if your tests use the
          # sqlproof.contrib.plpgsql_coverage helpers.
          psql "$SQLPROOF_DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS plpgsql_check;"

          # Apply GoTrue migration 20220224000811_update_auth_functions
          # so auth.uid() / .role() / .email() coalesce both the singular
          # and JSON GUC patterns (matching managed Supabase semantics).
          # Source: https://github.com/supabase/auth/blob/master/migrations/20220224000811_update_auth_functions.up.sql
          psql -v ON_ERROR_STOP=1 "$SQLPROOF_DATABASE_URL" <<'SQL'
          CREATE OR REPLACE FUNCTION auth.uid()
          RETURNS uuid LANGUAGE sql STABLE AS $$
            SELECT COALESCE(
              NULLIF(current_setting('request.jwt.claim.sub', true), ''),
              (NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
            )::uuid
          $$;

          CREATE OR REPLACE FUNCTION auth.role()
          RETURNS text LANGUAGE sql STABLE AS $$
            SELECT COALESCE(
              NULLIF(current_setting('request.jwt.claim.role', true), ''),
              (NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')
            )::text
          $$;

          CREATE OR REPLACE FUNCTION auth.email()
          RETURNS text LANGUAGE sql STABLE AS $$
            SELECT COALESCE(
              NULLIF(current_setting('request.jwt.claim.email', true), ''),
              (NULLIF(current_setting('request.jwt.claims', true), '')::jsonb ->> 'email')
            )::text
          $$;

          CREATE OR REPLACE FUNCTION auth.jwt()
          RETURNS jsonb LANGUAGE sql STABLE AS $$
            SELECT COALESCE(
              NULLIF(current_setting('request.jwt.claim', true), ''),
              NULLIF(current_setting('request.jwt.claims', true), '')
            )::jsonb
          $$;
          SQL

      - run: pip install --pre sqlproof pytest
      - run: pytest
```

That's the full Supabase recipe. You can drop this directly into a
Supabase project's repo and your RLS tests will work.

## Variations

### Reuse the Supabase CLI's database

If your tests run locally against `supabase start` (which boots Postgres on
`127.0.0.1:54322`), use `SUPABASE_DB_URL` instead of `SQLPROOF_DATABASE_URL`
so the same env var works both locally and in CI:

```bash
# Local dev
export SUPABASE_DB_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres
pytest

# In CI, set SUPABASE_DB_URL in env: block the same way
```

SqlProof's pytest plugin reads `SUPABASE_DB_URL` as a third fallback after
`--sqlproof-database-url` and `SQLPROOF_DATABASE_URL`.

### Multiple Python versions

Add a matrix strategy. The `services.postgres` block stays the same — it
boots once per matrix job:

```yaml
strategy:
  matrix:
    python-version: ["3.11", "3.12", "3.13"]
```

### Coverage gating

If you want to enforce a coverage threshold like SqlProof itself does:

```yaml
- run: pytest --cov=your_package --cov-fail-under=80
```

## Troubleshooting

**`auth.uid()` returns NULL in my tests** — you're missing the
GoTrue migration step from Shape 3 above, or you're using
`postgres:16` instead of `supabase/postgres`. RLS policies that call
`auth.uid()` will evaluate as "not authenticated" without it.

**Tests skip with "set SQLPROOF_DATABASE_URL to run Postgres integration tests"** —
the env var isn't reaching the test step. Check it's in the job's
top-level `env:` block (not inside an individual step) and the workflow has
been pushed.

**`plpgsql_check extension is not installed`** — only an issue if your
tests use `sqlproof.contrib.plpgsql_coverage`. Add the `CREATE EXTENSION
IF NOT EXISTS plpgsql_check` step from Shape 3, even if the rest of your
setup is vanilla.

**Service container takes too long to start** — increase
`--health-retries` in the `services.postgres.options`. The
`supabase/postgres` image is ~600 MB; cold pulls on a fresh runner can
take ~30 seconds before the healthcheck even starts probing.

**`pip install sqlproof` says "no matching distribution"** — add `--pre`.
SqlProof is in alpha and PyPI hides prereleases from default installs.

## Where to go from here

- **[Supabase quickstart](/supabase-quickstart/)** — the 60-second path for
  Supabase founders, including agent rules for AI-driven test writing.
- **[RLS testing patterns](/guides/supabase/)** — using `as_rls_user`,
  seeding `auth.users`, asserting policy behavior.
- **[Custom column generators](/guides/custom-generators/)** — when the
  default value generators don't match your domain.
