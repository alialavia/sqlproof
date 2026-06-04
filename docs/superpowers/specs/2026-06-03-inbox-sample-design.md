# Inbox sample database — design spec

**Status:** Draft (awaiting user review)
**Date:** 2026-06-03
**Author:** brainstormed with Claude

## Problem

SqlProof's existing examples (`examples/ecommerce`, `examples/orders`,
`examples/ripenn_scoring`, `examples/supabase_rls`) are each tightly
scoped to one teaching point — small schemas, one or two tests, focused
on a single property pattern. There is no single sample that:

1. Looks like a realistic, multi-feature Supabase application (RLS,
   pgvector, RPCs, triggers — not toy CRUD).
2. Backs a *recipe-style* documentation series where every recipe page
   builds on the same schema, so readers don't context-switch between
   shapes.
3. Demonstrates classes of bugs that pass human review *and* pass
   conventional example-based tests, but a SqlProof property catches —
   making the case for property-based testing concrete with
   reproducible counterexamples.

## Goal

Ship a single mid-sized Supabase-shaped sample (`examples/inbox/`) that
backs a new section of the documentation site
(`docs/examples/inbox/`). The sample's schema contains nine intentional,
realistic bugs in its RPCs / RLS policies / trigger (including one
sequence-dependent bug that only a stateful test surfaces, and two
write-side RLS bugs — mass assignment and missing DELETE — that round
out coverage of the most-cited Supabase RLS bug classes), plus one
*equivalence-pattern* recipe demonstrating safe query optimization.
Each bug is the subject of one recipe page following an identical
structure (problem → buggy code → passing example test → failing
SqlProof property → counterexample → fix). Readers can clone the repo,
apply migrations, watch tests fail, apply fix migrations, and watch
them pass — one bug at a time.

## Non-goals

- Replacing or restructuring the existing four examples. The inbox
  sample is additive.
- Real Supabase Storage integration. (Reasoning: SqlProof tests
  Postgres-level invariants; storage blobs are opaque to it. RLS on
  `storage.objects` is the only Postgres-level surface, and that is
  better demoed as a focused snippet than as a recipe in this sample.
  Revisit in v2 if a strong recipe emerges.)
- A Makefile, justfile, or seed-data SQL. Six bash lines in the README
  is enough setup documentation; property tests generate their own
  datasets and don't depend on seeds.
- A `tests/conftest.py`. SqlProof's pytest plugin already supplies
  `supabase_proof` / `supabase_db` / `proof` / `db` fixtures.

## Domain: AI customer support inbox

A multi-tenant support tool. Organizations receive tickets from
customers; agents in the org reply; AI-assisted features (similar-ticket
suggestion, KB hybrid search) use pgvector embeddings. Familiar shape
across SaaS; rich enough to host all five property patterns.

### Tables (`examples/inbox/schema/001_initial.sql`)

**Tenancy & identity**
- `organizations` — `id` (uuid pk), `name`, `sla_tier` enum
  (`bronze`/`silver`/`gold`)
- `org_members` — composite pk `(org_id, user_id)`, `role` enum
  (`admin`/`agent`/`viewer`); FK `user_id` → `auth.users(id)`
- `customers` — `id` (uuid pk), `email` (unique), `display_name`. Not
  `auth.users`; customers are external.

**Core domain**
- `tickets` — `id`, `org_id` FK, `customer_id` FK, `assigned_agent_id`
  nullable FK → `auth.users`, `status` enum
  (`open`/`pending`/`resolved`/`reopened`), `priority` enum
  (`low`/`med`/`high`/`urgent`), `subject`, `created_at`, `resolved_at`
  nullable, `sla_due_at`
- `messages` — `id`, `ticket_id` FK, `author_kind` enum
  (`customer`/`agent`/`system`), `author_user_id` nullable FK →
  `auth.users`, `is_internal` boolean default false, `body` text,
  `created_at`
- `ticket_events` — `id`, `ticket_id` FK, `event_type` enum
  (`status_change`/`assignment`/`tag_added`/`tag_removed`),
  `old_value` text, `new_value` text, `created_at`. Append-only.
- `tags` — `id`, `org_id` FK, `name`, unique `(org_id, name)`
- `ticket_tags` — composite pk `(ticket_id, tag_id)`

**AI / retrieval surface (pgvector)**
- `message_embeddings` — `id`, `message_id` FK, `chunk_index`,
  `embedding vector(384)`, unique `(message_id, chunk_index)`
- `kb_articles` — `id`, `org_id` FK, `title`, `body`, `published`
  boolean
- `kb_article_embeddings` — `id`, `article_id` FK, `chunk_index`,
  `embedding vector(384)`, unique `(article_id, chunk_index)`

Total: ten tables. Exercises composite PKs (`org_members`,
`ticket_tags`), enums, FKs to `auth.users` (external table for sqlproof
generation purposes), unique constraints, defaults, nullables, and the
pgvector type.

### Functions / triggers / policies (the bug surface)

`001_initial.sql` ships all of these together. The nine bug fixes ship
as separate numbered migrations (see Layout). Recipe 7 (equivalence)
ships v2 of one function as a later migration.

| # | Recipe | Surface | Bug | Property pattern |
|---|--------|---------|-----|------------------|
| 1 | `tenant-scoped-vector-search` | RPC `find_similar_tickets(ticket_id, k)` | Missing `WHERE org_id = ...` filter; returns nearest neighbors across all tenants | RLS regression |
| 2 | `correlated-rls-subqueries` | RLS policy `"agents see org tickets"` on `tickets` | `EXISTS (SELECT 1 FROM org_members WHERE user_id = auth.uid())` — subquery never correlates to `tickets.org_id`, so any org member sees every org's tickets | RLS regression |
| 3 | `idempotent-status-triggers` | Trigger `tg_close_sets_resolved_at` on `tickets` | Sets `NEW.resolved_at = now()` when `NEW.status = 'resolved'` but doesn't check `OLD.status IS DISTINCT FROM 'resolved'`; non-status edits to a resolved ticket bump `resolved_at` | Idempotency |
| 4 | `outer-joins-and-where` | RPC `organization_dashboard(org_id)` | `LEFT JOIN tickets t ... WHERE t.org_id = $1` — the WHERE collapses LEFT to INNER, dropping zero-count status buckets | Aggregation invariant |
| 5 | `internal-message-rls` | RLS policy on `messages` | Policy gates on parent-ticket visibility but doesn't gate `is_internal = true`; customers see agent-only internal notes on their own tickets | RLS regression |
| 6 | `stable-vector-pagination` | RPC `search_kb_hybrid(org_id, query_text, query_embedding, limit, offset)` | `ORDER BY combined_score DESC LIMIT k OFFSET o` with no tiebreaker; tied scores cause duplicates and gaps across pages | Round-trip / paginated set equality |
| 7 | `equivalent-query-optimization` | RPC `agent_workload_summary(org_id)` v1 vs v2 | v1 uses one correlated subquery per metric (slow); v2 uses single LEFT JOIN with `FILTER` (fast). v2 returns `NULL` for agents with zero tickets, v1 returns `0`. Hypothesis surfaces the divergence on the first run | Equivalence / migration safety |
| 8 | `stateful-ticket-lifecycle` | RPC `reopen_ticket(ticket_id)` | Sets `status = 'reopened'` but forgets to clear `resolved_at`. Single-shot property tests miss this because the invariant ("non-resolved status → resolved_at IS NULL") holds at every individual state — only the transition resolve→reopen leaves a stale value | Stateful (sequence-dependent) |
| 9 | `mass-assignment-without-with-check` | RLS policy `"members manage their own row"` on `org_members` | `FOR UPDATE USING (auth.uid() = user_id)` without `WITH CHECK` — an agent can `UPDATE org_members SET role = 'admin' WHERE user_id = auth.uid()` and self-promote. Reviewers see the USING clause and assume the policy is restrictive | RLS regression (write side) |
| 10 | `missing-delete-policy` | RLS on `org_members` — SELECT and UPDATE policies exist, DELETE policy does not | PostgREST permits the DELETE; viewers can self-eject from orgs (and admins can be ejected by viewers who craft the request). Reviewers focused on SELECT visibility miss the absent write policy | RLS regression (write side) |

Coverage check: pattern 1 (aggregation) → #4; pattern 2 (RLS) → #1,
#2, #5, #9, #10 (with #9 and #10 specifically covering write-side RLS
— mass assignment and missing DELETE — that the read-side recipes
don't reach); pattern 3 (migration / equivalence) → #7; pattern 4
(idempotency) → #3; pattern 5 (round-trip / serialization) → #6
(paginated-set equality is the same shape as serialize→parse equality);
plus stateful / sequence-dependent → #8 (the `SqlProofStateMachine`
pattern from AGENTS.md Pattern 3); plus a pgvector-specific shape (#1,
#6). All five canonical property patterns hit, plus the stateful
pattern, plus the write-side RLS bug classes (mass assignment / missing
operation policy) that recurrent Supabase RLS-audit articles call out
as the highest-impact misses. Bug classes outside the inbox sample (over-
permissive `USING (true)`, UPDATE-without-SELECT silent fail,
`security_invoker` view bypass, `user_metadata` trust, infinite policy
recursion, schema-level audits like "RLS enabled on every public table")
are captured in a companion reference page, not as individual recipes
(see "Integration with existing docs" below).

### Schema design notes

- `auth.users` is referenced as an external table — same pattern as the
  existing `examples/supabase_rls/schema.sql`. SqlProof's
  `supabase_proof` fixture seeds the test-user pool and registers it.
- `customers` is *not* `auth.users` — this is realistic (support apps
  identify end users by email, not auth account) and gives the schema a
  non-auth FK to exercise.
- pgvector dimension is `384` (matches sentence-transformer `all-MiniLM-L6-v2`
  output; cheap to generate, realistic for KB-scale embeddings). The
  recipe doesn't require a real embedding model — random vectors are
  fine for the property tests; the recipe page can mention this.
- Every table uses `gen_random_uuid()` defaults for `id` so SqlProof's
  generator can elide them (consistent with `AGENTS.md` guidance on
  default-bearing columns).

## Recipes (docs site pages)

Ten pages under `website/src/content/docs/examples/inbox/`. Every
page follows the same structure so readers can skim across them:

1. **Problem** (~50 words) — what real engineering task leads to this
   code, what the engineer is trying to do
2. **The code** — the buggy production code, syntax-highlighted, ~10–20
   lines
3. **Why review misses it** (~50 words) — the mental model that lets a
   reviewer glide past the bug
4. **The example test that passes** — the conventional hand-rolled test
   an engineer would write, ~10 lines, deliberately seeded such that
   the bug doesn't trigger
5. **The SqlProof property** — ~10 lines using `dataset_strategy` and
   `supabase_proof` (or just `proof`/`db` when auth isn't needed)
6. **The counterexample** — the shrunk dataset SqlProof reports
   (literal JSON or text from `.sqlproof/failures/<name>.json`)
7. **The fix** — the corrected SQL, also ~10–20 lines, with a one-line
   diff highlight

Recipe 7 (`equivalent-query-optimization`) follows the same structure
but with one extra section after the fix:

8. **Lifecycle** — when to write equivalence tests, when to delete
   them. Spelled out as four phases: (a) local during the refactor PR,
   (b) CI PR check requiring green, (c) CI on main during the
   deprecation window (typically ~1 week) while callers migrate from
   v1 to v2, (d) deleted alongside v1. This framing is what
   distinguishes equivalence-shape properties (scaffolding, short-lived)
   from the forever-properties of recipes 1–6, #8, #9, and #10.

Recipe 8 (`stateful-ticket-lifecycle`) follows the same structure but
with one extra section after the fix:

8. **When to reach for state machines** — sequence-dependent bugs
   (resolve → reopen leaving stale `resolved_at`) cannot be caught by
   `@given` because every individual state satisfies the invariant.
   State machines are slower per example, so use them only when the
   bug truly requires an ordered sequence; for single-update
   assertions, write a property test instead (cross-link to recipe 3).
   Mirrors AGENTS.md's Pattern 3 anti-pattern callout, made concrete.

Recipes 9 (`mass-assignment-without-with-check`) and 10
(`missing-delete-policy`) follow the same structure, with one shared
extra section after the fix:

8. **Why write-side RLS bugs slip past reviewers** — RLS code review
   pattern-matches on `USING (...)` and assumes the policy is
   restrictive. Both bugs share the same blind spot (one is "missing
   `WITH CHECK`", the other is "missing the policy entirely") and both
   have the same testing remedy: assert the *post-state* of a malicious
   write, not just the return value. Cross-link between #9 and #10.

Each recipe page is ~200–300 words. Total docs payload: ~10 pages plus
one `index.md`.

### Index page (`docs/examples/inbox/index.md`)

- One-paragraph overview of the domain
- An entity-relationship diagram (Mermaid, ~10 lines) showing the ten
  tables
- The repro flow (the six bash commands a reader runs)
- A table linking the ten recipes, each with a one-line summary and
  its property pattern
- A note that this sample is intentionally buggy and is meant to be
  cloned and explored, not deployed

## Layout

```
examples/inbox/
├── README.md                                # ~one screen: overview, how to run
├── schema/
│   ├── 001_initial.sql                      # tables + the 7 buggy RPCs/policies/trigger + v1 of workload_summary
│   ├── 002_fix_similar_tickets.sql          # recipe 1 fix
│   ├── 003_fix_tickets_rls.sql              # recipe 2 fix
│   ├── 004_fix_resolved_at_trigger.sql      # recipe 3 fix
│   ├── 005_fix_dashboard.sql                # recipe 4 fix
│   ├── 006_fix_messages_rls.sql             # recipe 5 fix
│   ├── 007_fix_hybrid_search.sql            # recipe 6 fix
│   ├── 008_add_workload_summary_v2.sql      # recipe 7: ships v2 (subtly diverges on NULL handling)
│   ├── 009_fix_workload_summary_v2_nulls.sql # recipe 7 fix (COALESCE in v2)
│   ├── 010_fix_reopen_ticket.sql            # recipe 8 fix (clears resolved_at on reopen)
│   ├── 011_fix_org_members_with_check.sql   # recipe 9 fix (adds WITH CHECK preserving role)
│   └── 012_add_org_members_delete_policy.sql # recipe 10 fix (adds DELETE policy)
└── tests/
    ├── test_similar_tickets.py              # recipe 1
    ├── test_tickets_rls.py                  # recipe 2
    ├── test_resolved_at_trigger.py          # recipe 3
    ├── test_dashboard.py                    # recipe 4
    ├── test_messages_rls.py                 # recipe 5
    ├── test_hybrid_search.py                # recipe 6
    ├── test_workload_summary.py             # recipe 7
    ├── test_ticket_lifecycle.py             # recipe 8 (SqlProofStateMachine)
    ├── test_org_members_mass_assignment.py  # recipe 9
    └── test_org_members_delete_policy.py    # recipe 10

website/src/content/docs/examples/inbox/
├── index.md
├── tenant-scoped-vector-search.md           # recipe 1
├── correlated-rls-subqueries.md             # recipe 2
├── idempotent-status-triggers.md            # recipe 3
├── outer-joins-and-where.md                 # recipe 4
├── internal-message-rls.md                  # recipe 5
├── stable-vector-pagination.md              # recipe 6
├── equivalent-query-optimization.md         # recipe 7
├── stateful-ticket-lifecycle.md             # recipe 8
├── mass-assignment-without-with-check.md    # recipe 9
└── missing-delete-policy.md                 # recipe 10
```

### Why per-bug fix migrations (vs. one `fixed.sql`)

The reader's repro flow benefits: after `psql -f 001_initial.sql`,
`pytest examples/inbox/tests -v` produces nine failures (test_7 skips
because v2 doesn't exist yet; the state machine test for recipe 8 and
both write-side RLS tests for recipes 9 and 10 are among the failures).
The reader opens any one recipe, runs `psql -f schema/0NN_fix_*.sql`,
and that recipe's test goes green — one bug at a time. This matches how Supabase users actually ship
schema fixes (per-PR migration files), gives each recipe a fully
self-contained "watch it fail → apply the migration → watch it pass"
loop, and incidentally seeds a future "migration safety" recipe (prove
the fix doesn't change intent for any dataset). Cost: ~10 lines per
fix file.

### Recipe 7's two-file pattern

Recipe 7 is the only recipe that needs two extra files (`008_*`,
`009_*`), because the teaching beat is "add an optimization candidate,
prove it equivalent, fix the divergence." Reader flow:

1. After `001`, v1 of `agent_workload_summary` exists; `test_workload_summary`
   guards on function existence and skips because v2 isn't loaded yet.
2. `psql -f 008_add_workload_summary_v2.sql` — v2 exists; the
   equivalence test runs and *fails* with a counterexample (an agent
   with zero tickets: v1 returns `0`, v2 returns `NULL`).
3. Reader reads the recipe page, understands which behavior is the
   contract (v1's `0`, since callers consume the count).
4. `psql -f 009_fix_workload_summary_v2_nulls.sql` — patches v2 with
   `coalesce(...)`. Test goes green.

Critically the fix is to **v2**, not v1. This mirrors the realistic
"v1 is the contract; my new optimized version must preserve it"
workflow that recipe 7's lifecycle section discusses.

## Repro flow (README content)

```bash
# 1. Install
pip install sqlproof psycopg

# 2. Start a local Supabase-shaped Postgres
#    (or: docker run postgres:16 with pgvector + auth setup;
#    see website/docs/guides/ci-cd.md for the vanilla-Postgres recipe)
supabase start
export SUPABASE_DB_URL='postgresql://postgres:postgres@127.0.0.1:54322/postgres'

# 3. Load the initial (buggy) schema
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/001_initial.sql

# 4. Run the tests — 9 failures, 1 skipped (test_workload_summary
#    skips until v2 is loaded; the state-machine test for the
#    ticket-lifecycle bug and both write-side RLS tests are among
#    the failures)
pytest examples/inbox/tests -v

# 5. Pick a recipe (say recipe 2). Apply the fix.
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/003_fix_tickets_rls.sql

# 6. Rerun just that recipe's test — green.
pytest examples/inbox/tests/test_tickets_rls.py -v
```

For recipe 7, an extra step between 3 and 4: `psql ... -f 008_add_workload_summary_v2.sql`.

## Equivalence-test lifecycle (recipe 7 framing)

The recipe page's *Lifecycle* section makes explicit that equivalence
tests are **scaffolding for a refactor, not a forever-test**. The four
phases:

1. **Local, during the refactor PR.** Engineer writes v2 and the
   equivalence test in the same commit, iterates until Hypothesis
   can't find a divergence.
2. **CI on the PR (required check).** Hypothesis runs more examples
   than the engineer had patience for locally; the persisted
   counterexample database under `.sqlproof/failures/` travels with the
   PR.
3. **CI on `main` during the deprecation window.** v1 and v2 both
   shipped; callers gradually migrate from v1 to v2 (typical window:
   ~one week). The equivalence test runs on every commit during this
   window, protecting against someone tweaking v2 in an unrelated PR
   and silently breaking equivalence while callers are mid-migration.
4. **Deleted with v1.** Once callers are off v1 and v1 is dropped, the
   equivalence test goes too. Trying to keep v1 alive just to host the
   property creates perpetual dead code.

**Exceptions** where the test really is forever: deprecation views,
dual-writes, or compatibility shims maintained across a versioned API
boundary. The recipe mentions these briefly.

This framing is intentionally orthogonal to recipes 1–6, #8, #9, and
#10, which guard permanent invariants (RLS read- and write-side,
idempotency, aggregation correctness, sequence-dependent state).
Calling out the lifecycle difference is the recipe's most distinctive
teaching beat — readers should leave understanding *when* to reach for
equivalence properties as much as *how*.

## Integration with existing docs

- New top-level entry under `examples/` in the sidebar:
  **Inbox sample** (folder index + 10 recipe pages).
- Add a cross-reference paragraph at the bottom of
  `examples/property-patterns.md` pointing each of the five patterns to
  its inbox recipe.
- New reference page under `guides/`:
  **`guides/supabase-rls-bug-classes.md`** — a compact catalog of the
  RLS bug classes that don't get full inbox recipes (overly permissive
  `USING (true)`, UPDATE-without-SELECT silent fail, `security_invoker`
  view bypass, `user_metadata` trust, infinite policy recursion, plus
  the schema-level audits like "RLS enabled on every public table" and
  "every policy has a `TO` clause"). One paragraph per bug class with a
  5–10 line sqlproof property snippet, no standalone schema, no fix
  migrations — a pattern reference complementary to the inbox's case
  studies. Cross-linked from inbox recipes 1, 2, 5, 9, 10 and from the
  index page. Drafts the canonical "audit my Supabase project's RLS"
  list that the existing `guides/supabase.md` page currently doesn't
  cover at this depth.
- No other existing docs change. The existing four examples
  (`ecommerce`, `orders`, `ripenn_scoring`, `supabase_rls`) stay in
  place and remain valid for their narrow teaching points.

## Testing & dependencies

- New runtime deps in the inbox sample: pgvector (`CREATE EXTENSION
  vector`). Documented in the README, prerequisite for running.
- No changes to SqlProof's own package dependencies. The inbox sample
  consumes SqlProof's public API only.
- The ten test files are conventional `pytest` files using existing
  fixtures from SqlProof's pytest plugin (one of them uses
  `SqlProofStateMachine` for recipe 8; recipes 9 and 10 use
  `as_rls_user` from `sqlproof.contrib.supabase` so RLS is actually
  enforced for the write-side assertions). They are not added to
  SqlProof's own CI test suite — they are demonstration code, intended
  to be run by end users following the docs. (If the team wants
  CI-level smoke coverage of "the inbox sample at least starts up,"
  that's a separate v2 ticket.)

## Open questions

None blocking. Implementation can proceed after user approval.

## Out of scope (deferred to v2)

- Supabase Storage recipe (e.g., gated download URLs).
- A stateful test of the ticket lifecycle.
- A migration-safety recipe explicitly comparing the buggy version of a
  function to its fixed counterpart across the fix migrations.
- An optional CI smoke job that loads the inbox schema and runs its
  tests on every SqlProof CI build.
- A real embedding-model integration (the sample uses random vectors;
  this is fine for property tests but a "use sentence-transformers
  here" recipe is a natural follow-up).
