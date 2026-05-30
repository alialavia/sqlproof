---
title: Run SqlProof in your CI
description: Drop-in GitHub Actions workflow for testing your Supabase-shaped Postgres with SqlProof. Plus variations for vanilla Postgres and DB-less tests.
---

This page gives you a copy-paste GitHub Actions workflow for running
SqlProof against your project's database, with the smallest possible
amount of YAML to write yourself.

The default recipe assumes you're testing a **Supabase-shaped project**
— `auth.users`, RLS policies that call `auth.uid()`, the usual stack.
If you don't need any of that, scroll to [Variations](#variations).

## The recipe

Drop this into `.github/workflows/test.yml` in your project:

```yaml
name: Tests
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
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
      - uses: actions/setup-python@v6
        with:
          python-version: "3.11"
      - uses: alialavia/sqlproof/.github/actions/setup-supabase-test-db@main
        with:
          database-url: ${{ env.SQLPROOF_DATABASE_URL }}
      - run: pip install sqlproof pytest
      - run: pytest
```

That's the full file. About 25 lines.

In production, replace `@main` with a tagged release (e.g. `@v0.2.0`) so
you don't get surprised by upstream changes.

## What's in here

**Service container** (`services.postgres`):
- Uses the [`supabase/postgres`](https://hub.docker.com/r/supabase/postgres)
  image instead of the vanilla `postgres:16`. This image ships the `auth`
  schema (with `auth.users`, `auth.uid()`, etc.), the `plpgsql_check`
  extension binary, `pgvector`, `pgsodium`, `pgjwt`, and the rest of the
  extensions Supabase bundles. Pin to a specific tag — check
  [Docker Hub](https://hub.docker.com/r/supabase/postgres/tags) for newer
  releases.
- Healthcheck retries are generous (15 × 10s) because the Supabase image
  is ~600 MB; cold pulls on a fresh runner can take a while before the
  healthcheck even starts probing.

**Env var** (`SQLPROOF_DATABASE_URL`):
- SqlProof's pytest plugin reads this. The resolution order is
  `--sqlproof-database-url` flag → `SQLPROOF_DATABASE_URL` env →
  `SUPABASE_DB_URL` env. If none is set, tests using the `proof`/`db`
  fixtures skip cleanly (they don't fail).

**The composite action** (`alialavia/sqlproof/.github/actions/setup-supabase-test-db`):
- Installs the `plpgsql_check` extension (it ships with the image but
  isn't pre-installed in the default database).
- Applies [GoTrue's `20220224000811_update_auth_functions` migration](https://github.com/supabase/auth/blob/master/migrations/20220224000811_update_auth_functions.up.sql)
  so that `auth.uid()` / `auth.role()` / `auth.email()` accept the modern
  JSON `request.jwt.claims` GUC that PostgREST 8+ writes — matching
  managed Supabase semantics. **Without this step, `auth.uid()` silently
  returns NULL in your tests** and every RLS policy evaluates as "not
  authenticated."
- Optionally takes `verbose: 'true'` to print the resulting extension
  list and `auth` schema functions to the job log.

The SQL the action applies is committed in this repo at
[`.github/actions/setup-supabase-test-db/sql/supabase-test-init.sql`](https://github.com/alialavia/sqlproof/blob/main/.github/actions/setup-supabase-test-db/sql/supabase-test-init.sql).
Audit it before pinning a version.

## Variations

### Vanilla Postgres (no Supabase auth)

If your tests hit a real Postgres but don't use `auth.uid()` or any
Supabase-specific surface:

```yaml
services:
  postgres:
    image: postgres:16
    env:
      POSTGRES_PASSWORD: postgres
    ports: ["5432:5432"]
    options: >-
      --health-cmd "pg_isready -U postgres"
      --health-interval 10s
      --health-timeout 5s
      --health-retries 10
env:
  SQLPROOF_DATABASE_URL: postgresql://postgres:postgres@127.0.0.1:5432/postgres
```

Skip the `setup-supabase-test-db` step entirely — you don't need it.

### No database at all

If your tests only use the in-memory client or the pure data generator
(no `proof.check(...)` against a live DB), drop both the service
container and the env var:

```yaml
- run: pip install sqlproof pytest
- run: pytest
```

### Multiple Python versions

The `services.postgres` block boots once per matrix job, so this just
works:

```yaml
strategy:
  matrix:
    python-version: ["3.11", "3.12", "3.13"]
```

### Local dev with the Supabase CLI

`supabase start` brings up Postgres on `127.0.0.1:54322`. The pytest
plugin's third fallback is `SUPABASE_DB_URL`, so you can use the same
env var for both local and CI:

```bash
# Local dev
export SUPABASE_DB_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres
pytest

# In CI, set SUPABASE_DB_URL in the env: block instead of SQLPROOF_DATABASE_URL
```

## Troubleshooting

**`auth.uid()` returns NULL in my tests** — you're either not using the
`setup-supabase-test-db` action, or you're running it against a plain
`postgres:16` image (which has no `auth` schema at all). The Supabase
image PLUS the composite action are both required.

**Tests skip with "set SQLPROOF_DATABASE_URL to run Postgres integration
tests"** — the env var isn't reaching the test step. Check it's in the
job's top-level `env:` block, not inside an individual step.

**`Error: ENOENT: no such file or directory, scandir '...github/actions/setup-supabase-test-db'`** —
you're referencing the action at a ref where it doesn't exist yet
(introduced in 0.2.0). Either pin to `@main`, or to `@v0.2.0` or later.

**Service container takes too long to start** — increase
`--health-retries` in `services.postgres.options`. The Supabase image is
heavier than `postgres:16`; first-time pulls can take ~30 seconds before
the healthcheck loop even begins.

## Where to go from here

- **[Supabase quickstart](/supabase-quickstart/)** — the 60-second path
  for Supabase founders, including agent rules for AI-driven test
  writing.
- **[RLS testing patterns](/guides/supabase/)** — using `as_rls_user`,
  seeding `auth.users`, asserting policy behavior.
- **[Custom column generators](/guides/custom-generators/)** — when the
  default value generators don't match your domain.
