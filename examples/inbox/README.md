# Inbox sample

A multi-tenant Supabase-shaped customer-support inbox: organizations,
tickets, agents, messages, KB articles, plus pgvector embeddings for
similarity search.

Every RPC, policy, and trigger in `schema/001_initial.sql` contains
exactly one intentional, realistic bug. Each fix is a separate
numbered migration. Each bug has a recipe page under
[`docs/examples/inbox/`](https://sqlproof.com/examples/inbox/)
walking through: the production code, the example test that misses
the bug, the SqlProof property that catches it, and the fix.

## Run it

```bash
# 1. Install
pip install sqlproof psycopg

# 2. Start a Supabase-shaped Postgres (with pgvector + auth schema)
supabase start
export SUPABASE_DB_URL='postgresql://postgres:postgres@127.0.0.1:54322/postgres'

# 3. Load the initial (buggy) schema
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/001_initial.sql

# 4. Run the tests — 9 failures, 2 skipped
pytest examples/inbox/tests -v

# 5. Pick a recipe (say recipe 2). Apply its fix.
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/003_fix_tickets_rls.sql

# 6. Rerun just that recipe's test
pytest examples/inbox/tests/test_tickets_rls.py -v
```

For recipe 7 (equivalence), there's an extra step between 3 and 4:

```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/008_add_workload_summary_v2.sql
```

## Recipes

See [`docs/examples/inbox/index.md`](https://sqlproof.com/examples/inbox/)
for the full catalog.

## Mutation scoring (recipe 11)

Once all fixes are applied, `tests/mutation/` re-introduces each
recipe's bug as a mutant and requires the property suite to kill it.
It needs a dedicated template database with zero open connections
(a test container — not `supabase start`, whose services stay
connected). From the sqlproof repo root:

```bash
# Supabase-shaped Postgres test container (same image as CI)
docker run -d --name sqlproof-pg -e POSTGRES_PASSWORD=postgres \
  -p 54399:5432 supabase/postgres:15.8.1.040
until docker exec sqlproof-pg pg_isready -U postgres >/dev/null 2>&1; do sleep 2; done
docker exec -i sqlproof-pg psql -v ON_ERROR_STOP=1 -U postgres -d postgres \
  < .github/actions/setup-supabase-test-db/sql/supabase-test-init.sql

# Template: clone the auth-bearing postgres DB. The image's pg_cron/pg_net
# workers hold sessions on it and the postgres role can't terminate them,
# so terminate-and-clone in one supabase_admin session:
docker exec -e PGPASSWORD=postgres sqlproof-pg psql -U supabase_admin -d template1 \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
      WHERE datname='postgres' AND pid <> pg_backend_pid()" \
  -c 'CREATE DATABASE inbox_proof_template TEMPLATE postgres' \
  -c 'ALTER DATABASE inbox_proof_template OWNER TO postgres'

for f in examples/inbox/schema/*.sql; do
  docker exec -i sqlproof-pg psql -v ON_ERROR_STOP=1 -U postgres \
    -d inbox_proof_template < "$f"
done

export SQLPROOF_TEMPLATE_URL='postgresql://postgres:postgres@127.0.0.1:54399/inbox_proof_template'
uv run pytest examples/inbox/tests/mutation -m mutation -v
```

Walkthrough: [Scoring the suite with mutation
testing](https://sqlproof.com/examples/inbox/mutation-scoring/).
