---
title: The Inbox sample
description: A multi-tenant Supabase support inbox with ten recipes covering RLS, pgvector, triggers, aggregation, equivalence-pattern refactors, and stateful tests.
---

A multi-tenant customer-support inbox: organizations, agents, customers, tickets, messages, KB articles, and pgvector embeddings for similarity search. Every recipe page on this section pairs a buggy implementation with a SqlProof property that catches it — and a fix migration you can apply to watch the property go green.

## Schema

```
organizations
  └── org_members ─→ auth.users
  └── tickets
        ├── customers
        ├── messages
        │   └── message_embeddings  (vector(384))
        ├── ticket_events
        └── ticket_tags ─→ tags
  └── kb_articles
        └── kb_article_embeddings   (vector(384))
```

Ten tables. Source: [examples/inbox/schema/001_initial.sql](https://github.com/alialavia/sqlproof/blob/main/examples/inbox/schema/001_initial.sql).

## Run it

```bash
pip install sqlproof psycopg
supabase start
export SUPABASE_DB_URL='postgresql://postgres:postgres@127.0.0.1:54322/postgres'
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/001_initial.sql
pytest examples/inbox/tests -v    # 9 failures + 1 skipped
```

Apply any fix migration to watch one recipe go green:

```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/003_fix_tickets_rls.sql
pytest examples/inbox/tests/test_tickets_rls.py -v
```

## Recipes

| Recipe | Property pattern | Bug summary |
|--------|------------------|-------------|
| [Vector search leaks across tenants](tenant-scoped-vector-search) | RLS regression | `SECURITY DEFINER` similarity-search RPC missing `org_id` filter |
| [Correlated RLS subqueries](correlated-rls-subqueries) | RLS regression | `EXISTS` subquery doesn't correlate to parent row |
| [Non-idempotent status trigger](idempotent-status-triggers) | Idempotency | Trigger fires on every edit, not just on transitions |
| [LEFT JOIN collapsed by WHERE](outer-joins-and-where) | Aggregation | Dashboard drops zero-bucket status rows |
| [Internal messages leak to customers](internal-message-rls) | RLS regression | Policy doesn't gate `is_internal = true` on customer path |
| [Pagination breaks on tied scores](stable-vector-pagination) | Round-trip (paginated set equality) | `ORDER BY score` has no tiebreaker (+ JOIN fanout when articles have multiple embedding chunks) |
| [Equivalent query optimization](equivalent-query-optimization) | Equivalence / migration safety | INNER JOIN drops zero-ticket agents that v1's correlated-subquery shape preserves |
| [Stateful ticket lifecycle](stateful-ticket-lifecycle) | Stateful (sequence-dependent) | `reopen_ticket` doesn't clear `resolved_at` |
| [Mass assignment without WITH CHECK](mass-assignment-without-with-check) | RLS regression (write side) | UPDATE policy lets members change any column of their own row |
| [Overly permissive DELETE policy](missing-delete-policy) | RLS regression (write side) | DELETE policy with `USING (true)` lets viewers eject admins |

For smaller RLS bug classes that don't justify a full case study (over-permissive `USING (true)`, UPDATE-without-SELECT silent fail, `security_invoker` view bypass, `user_metadata` trust, infinite policy recursion, plus schema-level audits like "RLS enabled on every public table"), see the [Supabase RLS bug classes](/guides/supabase-rls-bug-classes/) reference page.

## Caveats

- The buggy code in this sample is intentional. **Do not deploy this schema as-is**.
- Embeddings in tests are random — these recipes test schema-level invariants, not retrieval quality. Plugging in a real embedding model is a separate concern.
- Recipes 1 and 6 depend on a pgvector parser workaround (`vector_strategy(384)` in `tests/_helpers.py`) until [SqlProof issue #69](https://github.com/alialavia/sqlproof/issues/69) lands.
