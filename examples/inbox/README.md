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

# 4. Run the tests — 9 failures, 1 skipped
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
