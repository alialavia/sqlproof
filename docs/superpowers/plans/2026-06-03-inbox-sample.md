# Inbox Sample Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `examples/inbox/` — a multi-tenant Supabase customer-support inbox with intentional, realistic bugs in nine RPCs/policies/triggers plus one equivalence-pattern refactor (ten total recipes), backing a new docs section at `docs/examples/inbox/` and a companion reference page at `docs/guides/supabase-rls-bug-classes.md`.

**Architecture:** One mid-sized PostgreSQL schema (ten tables) with all buggy code shipped in `001_initial.sql`. Each recipe's fix is a separate numbered migration (`002_*` … `012_*`). Readers run the test suite, see failures, apply the fix migration for one recipe at a time, and watch that recipe go green. Each recipe page in the docs follows the same six-section template (problem → buggy code → why review misses it → example test → SqlProof property → counterexample → fix). The Astro/Starlight docs site picks up new `.md` files automatically once they're added to the sidebar config.

**Tech Stack:** Python 3.11+, SqlProof (this repo), psycopg, Hypothesis, pytest, PostgreSQL 15+ with pgvector and pgcrypto extensions, Astro/Starlight for docs.

---

## File Structure

Files created or modified by this plan:

```
examples/inbox/
├── README.md                                  # Task 1
├── schema/
│   ├── 001_initial.sql                        # Task 1 (tables only); appended in tasks 2-11
│   ├── 002_fix_similar_tickets.sql            # Task 10 (recipe 1 fix)
│   ├── 003_fix_tickets_rls.sql                # Task 2 (recipe 2 fix)
│   ├── 004_fix_resolved_at_trigger.sql        # Task 4 (recipe 3 fix)
│   ├── 005_fix_dashboard.sql                  # Task 5 (recipe 4 fix)
│   ├── 006_fix_messages_rls.sql               # Task 3 (recipe 5 fix)
│   ├── 007_fix_hybrid_search.sql              # Task 11 (recipe 6 fix)
│   ├── 008_add_workload_summary_v2.sql        # Task 9 (recipe 7 addition)
│   ├── 009_fix_workload_summary_v2_nulls.sql  # Task 9 (recipe 7 fix)
│   ├── 010_fix_reopen_ticket.sql              # Task 8 (recipe 8 fix)
│   ├── 011_fix_org_members_with_check.sql     # Task 6 (recipe 9 fix)
│   └── 012_add_org_members_delete_policy.sql  # Task 7 (recipe 10 fix)
└── tests/
    ├── _helpers.py                            # Task 1 (vector_strategy)
    ├── test_similar_tickets.py                # Task 10 (recipe 1)
    ├── test_tickets_rls.py                    # Task 2 (recipe 2)
    ├── test_resolved_at_trigger.py            # Task 4 (recipe 3)
    ├── test_dashboard.py                      # Task 5 (recipe 4)
    ├── test_messages_rls.py                   # Task 3 (recipe 5)
    ├── test_hybrid_search.py                  # Task 11 (recipe 6)
    ├── test_workload_summary.py               # Task 9 (recipe 7)
    ├── test_ticket_lifecycle.py               # Task 8 (recipe 8 — state machine)
    ├── test_org_members_mass_assignment.py    # Task 6 (recipe 9)
    └── test_org_members_delete_policy.py      # Task 7 (recipe 10)

website/src/content/docs/
├── examples/inbox/
│   ├── index.md                                # Task 12
│   ├── tenant-scoped-vector-search.md          # Task 10 (recipe 1)
│   ├── correlated-rls-subqueries.md            # Task 2 (recipe 2)
│   ├── idempotent-status-triggers.md           # Task 4 (recipe 3)
│   ├── outer-joins-and-where.md                # Task 5 (recipe 4)
│   ├── internal-message-rls.md                 # Task 3 (recipe 5)
│   ├── stable-vector-pagination.md             # Task 11 (recipe 6)
│   ├── equivalent-query-optimization.md        # Task 9 (recipe 7)
│   ├── stateful-ticket-lifecycle.md            # Task 8 (recipe 8)
│   ├── mass-assignment-without-with-check.md   # Task 6 (recipe 9)
│   └── missing-delete-policy.md                # Task 7 (recipe 10)
└── guides/
    └── supabase-rls-bug-classes.md             # Task 12 (reference page)

website/astro.config.mjs                        # Task 13 (sidebar additions)
website/src/content/docs/examples/property-patterns.md  # Task 13 (cross-refs)
```

## Prerequisites & Workarounds

**Postgres environment.** Tests require PostgreSQL 15+ with the `vector` and `pgcrypto` extensions and a Supabase-shaped `auth.users` table. The setup mirrors `examples/supabase_rls/`. Two ways to provide it:

1. **Local Supabase**: `supabase start`; export `SUPABASE_DB_URL='postgresql://postgres:postgres@127.0.0.1:54322/postgres'`.
2. **Vanilla Postgres + pgvector image**: `docker run -p 5432:5432 -e POSTGRES_PASSWORD=postgres pgvector/pgvector:pg16` and apply [GoTrue's auth migration](https://github.com/supabase/auth/blob/master/migrations/20220224000811_update_auth_functions.up.sql) plus `CREATE EXTENSION vector`.

**pgvector schema-parser gap.** SqlProof's schema parser currently treats `vector(N)` columns as fallback `text` and the row generator produces strings that PostgreSQL rejects ([issue #69](https://github.com/alialavia/sqlproof/issues/69)). Workaround: every test that touches a table with a `vector` column passes a column override that generates a properly formatted vector literal string. The helper `vector_strategy(dim)` in `examples/inbox/tests/_helpers.py` (Task 1) returns a Hypothesis strategy producing strings like `'[0.1,0.2,...]'` of the right dimension. When #69 lands, these overrides can be removed.

**Skip pattern for recipe 7.** Recipe 7's test (`test_workload_summary.py`) asserts equivalence between `agent_workload_summary_v1` and `agent_workload_summary_v2`. The v2 function doesn't exist until `008_add_workload_summary_v2.sql` is applied. The test starts with a `to_regprocedure('public.agent_workload_summary_v2(uuid)') IS NOT NULL` guard that calls `pytest.skip(...)` when v2 isn't present.

---

## Task ordering rationale

Tasks are ordered to minimize blockers and let later recipes build on earlier patterns:

1. **Foundation** (Task 1) — tables only, no buggy code yet
2. **Pure RLS recipes** (Tasks 2–3) — establish the RLS-test pattern with simple cases
3. **Pure SQL recipes** (Tasks 4–5) — trigger + aggregation, no auth needed
4. **Write-side RLS recipes** (Tasks 6–7) — variant of the RLS-test pattern
5. **Stateful recipe** (Task 8) — introduces SqlProofStateMachine
6. **Equivalence recipe** (Task 9) — slightly different shape (two-file pattern)
7. **pgvector recipes** (Tasks 10–11) — applied last so the team can confirm the workaround works once
8. **Reference page + index** (Task 12)
9. **Docs site integration** (Task 13)

File numbering follows the spec (002 = recipe 1 fix, 003 = recipe 2 fix, etc.) regardless of task order.

---

### Task 1: Foundation — schema tables, README, helpers, smoke test

**Files:**
- Create: `examples/inbox/README.md`
- Create: `examples/inbox/schema/001_initial.sql` (tables only; RPCs/policies/trigger appended in later tasks)
- Create: `examples/inbox/tests/_helpers.py`
- Create: `examples/inbox/tests/test_smoke.py`

- [ ] **Step 1: Create the directory structure**

Run:
```bash
mkdir -p examples/inbox/schema examples/inbox/tests
```

- [ ] **Step 2: Write the base schema (tables only) to `examples/inbox/schema/001_initial.sql`**

```sql
-- Inbox sample: tables, RLS enablement, and grants.
--
-- This file ships ten tables plus the buggy RPCs, RLS policies, and
-- trigger that the recipes' tests target. Each recipe's fix lives in
-- a separate migration (002_*, 003_*, ...). The buggy items are
-- appended at the bottom by later tasks; this initial section is just
-- the schema shape.
--
-- Assumes a Supabase-shaped database where these already exist:
--   * `auth.users` (table)
--   * `auth.uid()` (function)
--   * `authenticated` (role)
-- Plus the `vector` and `pgcrypto` extensions.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

CREATE TYPE sla_tier         AS ENUM ('bronze', 'silver', 'gold');
CREATE TYPE member_role      AS ENUM ('admin', 'agent', 'viewer');
CREATE TYPE ticket_status    AS ENUM ('open', 'pending', 'resolved', 'reopened');
CREATE TYPE ticket_priority  AS ENUM ('low', 'med', 'high', 'urgent');
CREATE TYPE message_author_kind AS ENUM ('customer', 'agent', 'system');
CREATE TYPE event_type       AS ENUM ('status_change', 'assignment', 'tag_added', 'tag_removed');

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

CREATE TABLE organizations (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name      TEXT NOT NULL,
    sla_tier  sla_tier NOT NULL DEFAULT 'bronze'
);

CREATE TABLE org_members (
    org_id   UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id  UUID NOT NULL REFERENCES auth.users(id)    ON DELETE CASCADE,
    role     member_role NOT NULL,
    PRIMARY KEY (org_id, user_id)
);

CREATE TABLE customers (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL
);

CREATE TABLE tickets (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id             UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    customer_id        UUID NOT NULL REFERENCES customers(id),
    assigned_agent_id  UUID REFERENCES auth.users(id),
    status             ticket_status NOT NULL DEFAULT 'open',
    priority           ticket_priority NOT NULL DEFAULT 'med',
    subject            TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at        TIMESTAMPTZ,
    sla_due_at         TIMESTAMPTZ NOT NULL
);

CREATE TABLE messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id       UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author_kind     message_author_kind NOT NULL,
    author_user_id  UUID REFERENCES auth.users(id),
    is_internal     BOOLEAN NOT NULL DEFAULT false,
    body            TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE ticket_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id   UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    event_type  event_type NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE tags (
    id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id  UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    UNIQUE (org_id, name)
);

CREATE TABLE ticket_tags (
    ticket_id  UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    tag_id     UUID NOT NULL REFERENCES tags(id)    ON DELETE CASCADE,
    PRIMARY KEY (ticket_id, tag_id)
);

CREATE TABLE message_embeddings (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id   UUID NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    embedding    vector(384) NOT NULL,
    UNIQUE (message_id, chunk_index)
);

CREATE TABLE kb_articles (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id     UUID NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    published  BOOLEAN NOT NULL DEFAULT false
);

CREATE TABLE kb_article_embeddings (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    article_id   UUID NOT NULL REFERENCES kb_articles(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL,
    embedding    vector(384) NOT NULL,
    UNIQUE (article_id, chunk_index)
);

-- ---------------------------------------------------------------------------
-- Grants (RLS will gate visibility once policies are added below)
-- ---------------------------------------------------------------------------

GRANT SELECT, INSERT, UPDATE, DELETE ON organizations           TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON org_members             TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON customers               TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON tickets                 TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON messages                TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON ticket_events           TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON tags                    TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON ticket_tags             TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON message_embeddings      TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON kb_articles             TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON kb_article_embeddings   TO authenticated;

-- ---------------------------------------------------------------------------
-- Buggy RPCs, policies, and triggers are appended below by recipe tasks.
-- ---------------------------------------------------------------------------
```

- [ ] **Step 3: Write the helpers module to `examples/inbox/tests/_helpers.py`**

```python
"""Shared test helpers for the inbox sample.

Currently exports `vector_strategy(dim)` — a workaround for SqlProof
issue #69 (the schema parser doesn't yet recognise `vector(N)` columns,
so we override embedding columns with a strategy that emits Postgres
vector-literal strings of the right dimension).
"""

from __future__ import annotations

from hypothesis import strategies as st


def vector_strategy(dim: int) -> st.SearchStrategy[str]:
    """Generate a Postgres vector literal of the given dimension.

    Returns strings shaped like ``'[0.123,-0.456,...]'`` that PostgreSQL's
    `vector` type accepts directly. Components are bounded to a small
    range so generated vectors stay reasonable for cosine/L2 distance
    computations during property tests.
    """
    component = st.floats(
        min_value=-1.0,
        max_value=1.0,
        allow_nan=False,
        allow_infinity=False,
        width=32,
    )
    return st.lists(component, min_size=dim, max_size=dim).map(
        lambda xs: "[" + ",".join(f"{x:.6f}" for x in xs) + "]",
    )
```

- [ ] **Step 4: Write the smoke test to `examples/inbox/tests/test_smoke.py`**

```python
"""Smoke test: schema loads, every public table exists, no orphan FKs."""

from __future__ import annotations


EXPECTED_TABLES = {
    "organizations",
    "org_members",
    "customers",
    "tickets",
    "messages",
    "ticket_events",
    "tags",
    "ticket_tags",
    "message_embeddings",
    "kb_articles",
    "kb_article_embeddings",
}


def test_all_expected_tables_present(db) -> None:
    rows = db.query(
        """
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
        """,
    )
    present = {row["tablename"] for row in rows}
    missing = EXPECTED_TABLES - present
    assert not missing, f"missing tables: {sorted(missing)}"


def test_pgvector_extension_available(db) -> None:
    rows = db.query(
        "SELECT extname FROM pg_extension WHERE extname = 'vector'",
    )
    assert rows, "pgvector extension must be installed"
```

- [ ] **Step 5: Write the README to `examples/inbox/README.md`**

```markdown
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
```

- [ ] **Step 6: Apply the schema and run the smoke test**

Run:
```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/001_initial.sql
pytest examples/inbox/tests/test_smoke.py -v
```
Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add examples/inbox/README.md examples/inbox/schema/001_initial.sql examples/inbox/tests/_helpers.py examples/inbox/tests/test_smoke.py
git commit -m "feat(examples): inbox sample foundation — schema, helpers, smoke test"
```

---

### Task 2: Recipe 2 — correlated-rls-subqueries

**Files:**
- Modify: `examples/inbox/schema/001_initial.sql` (append RLS policies on `tickets` and a basic policy on `org_members`)
- Create: `examples/inbox/schema/003_fix_tickets_rls.sql`
- Create: `examples/inbox/tests/test_tickets_rls.py`
- Create: `website/src/content/docs/examples/inbox/correlated-rls-subqueries.md`

The buggy RLS policy on `tickets` uses an `EXISTS` subquery that never correlates back to `tickets.org_id`, so any org member sees every org's tickets.

- [ ] **Step 1: Append the buggy RLS policies to `001_initial.sql`**

Append at the end of the file:

```sql
-- ---------------------------------------------------------------------------
-- Recipe 2 (correlated-rls-subqueries) — BUGGY policies on tickets
-- ---------------------------------------------------------------------------

ALTER TABLE tickets       ENABLE ROW LEVEL SECURITY;
ALTER TABLE org_members   ENABLE ROW LEVEL SECURITY;

-- Org members can read members of their own orgs (used as a building
-- block by ticket policies; correctly correlated).
CREATE POLICY "members visible to org members" ON org_members
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM org_members om
      WHERE om.user_id = auth.uid() AND om.org_id = org_members.org_id
    )
  );

-- BUG: This policy says "any authenticated user who is a member of
-- ANY org can read ALL tickets." The EXISTS subquery filters
-- `org_members.user_id = auth.uid()` but never correlates back to
-- `tickets.org_id`. Reviewers skim past it because the shape "looks
-- like other RLS policies."
CREATE POLICY "agents see org tickets" ON tickets
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM org_members
      WHERE org_members.user_id = auth.uid()
    )
  );
```

- [ ] **Step 2: Write the failing test to `examples/inbox/tests/test_tickets_rls.py`**

```python
"""Recipe 2: correlated-rls-subqueries.

The "agents see org tickets" policy on `tickets` uses an EXISTS
subquery that filters by `auth.uid()` but never correlates to
`tickets.org_id`. Result: any member of any org can read every
org's tickets.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.contrib.supabase import as_rls_user

PROOF = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@PROOF
@given(data=st.data())
def test_member_of_org_a_cannot_read_tickets_in_org_b(
    supabase_proof, data,
) -> None:
    """Property: a viewer in org A sees zero rows from org B's tickets."""

    dataset = data.draw(
        supabase_proof.dataset_strategy(
            sizes={
                "organizations": 2,
                "org_members": 2,
                "customers": 2,
                "tickets": 2,
            },
        ),
    )
    with supabase_proof.client_for_dataset(dataset) as db:
        orgs = dataset["organizations"]
        members = dataset["org_members"]

        # Find a member of org A and a ticket in org B
        org_a_member = next(m for m in members if m["org_id"] == orgs[0]["id"])
        tickets_in_b = [t for t in dataset["tickets"] if t["org_id"] == orgs[1]["id"]]
        if not tickets_in_b:
            # Hypothesis happened to put all tickets in org A this run;
            # let the framework re-draw a different dataset.
            return

        with as_rls_user(db, org_a_member["user_id"]):
            visible = db.query(
                "SELECT id, org_id FROM tickets WHERE org_id = %s",
                orgs[1]["id"],
            )
        assert visible == [], (
            f"member of org A leaked tickets from org B: {visible}"
        )
```

- [ ] **Step 3: Run the test — expect failure**

Run:
```bash
pytest examples/inbox/tests/test_tickets_rls.py -v
```
Expected: FAIL with a Hypothesis counterexample where a member of org A reads org B's tickets.

- [ ] **Step 4: Write the fix migration to `examples/inbox/schema/003_fix_tickets_rls.sql`**

```sql
-- Recipe 2 fix: correlate the EXISTS subquery back to `tickets.org_id`.
--
-- Before: `EXISTS (SELECT 1 FROM org_members WHERE user_id = auth.uid())`
--         — fires true for any authenticated org member.
-- After:  `EXISTS (SELECT 1 FROM org_members WHERE user_id = auth.uid()
--                  AND org_id = tickets.org_id)` — fires true only when
--         the caller is a member of *this ticket's* org.

DROP POLICY "agents see org tickets" ON tickets;

CREATE POLICY "agents see org tickets" ON tickets
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM org_members
      WHERE org_members.user_id = auth.uid()
        AND org_members.org_id  = tickets.org_id
    )
  );
```

- [ ] **Step 5: Apply the fix and re-run the test**

Run:
```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/003_fix_tickets_rls.sql
pytest examples/inbox/tests/test_tickets_rls.py -v
```
Expected: PASS.

- [ ] **Step 6: Write the doc page to `website/src/content/docs/examples/inbox/correlated-rls-subqueries.md`**

```markdown
---
title: Correlated RLS subqueries
description: An EXISTS subquery in an RLS policy that doesn't correlate to the parent row leaks every tenant's data.
---

## Problem

You ship a SELECT policy on `tickets` so that "agents only see their org's tickets." Code-review goes fine; the local test passes. In production, an agent in a different org runs a list query and gets back every other org's tickets.

## The code (`schema/001_initial.sql`)

```sql
CREATE POLICY "agents see org tickets" ON tickets
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM org_members
      WHERE org_members.user_id = auth.uid()
    )
  );
```

## Why review misses it

The shape "EXISTS (SELECT 1 FROM org_members WHERE user_id = auth.uid())" reads as "is the caller a member?" — and reviewers pattern-match on that intent. The missing correlation back to `tickets.org_id` is invisible until you ask "a member of *which* org?".

## The example test that passes

```python
def test_agent_sees_their_org_tickets(db, org, ticket):
    with as_rls_user(db, org["owner_id"]):
        rows = db.query("SELECT id FROM tickets WHERE org_id = %s", org["id"])
    assert len(rows) == 1
```

One org, one ticket — the policy returns the row, the test is green. The cross-org leak only surfaces when the test data contains *two* distinct orgs.

## The SqlProof property

```python
@given(data=st.data())
def test_member_of_org_a_cannot_read_tickets_in_org_b(supabase_proof, data):
    dataset = data.draw(supabase_proof.dataset_strategy(
        sizes={"organizations": 2, "org_members": 2, "customers": 2, "tickets": 2},
    ))
    with supabase_proof.client_for_dataset(dataset) as db:
        orgs = dataset["organizations"]
        member_of_a = next(m for m in dataset["org_members"] if m["org_id"] == orgs[0]["id"])
        with as_rls_user(db, member_of_a["user_id"]):
            visible = db.query("SELECT id FROM tickets WHERE org_id = %s", orgs[1]["id"])
        assert visible == []
```

## The counterexample

```
Property failed: member of org A leaked tickets from org B
Dataset: {"organizations": 2, "org_members": 2, "tickets": 2}
Row context: org A user u1 read 1 ticket from org B
```

## The fix (`schema/003_fix_tickets_rls.sql`)

```sql
DROP POLICY "agents see org tickets" ON tickets;
CREATE POLICY "agents see org tickets" ON tickets
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM org_members
      WHERE org_members.user_id = auth.uid()
        AND org_members.org_id  = tickets.org_id   -- the missing line
    )
  );
```

One line. Two-org property tests catch every version of this bug.
```

- [ ] **Step 7: Commit**

```bash
git add examples/inbox/schema/001_initial.sql examples/inbox/schema/003_fix_tickets_rls.sql examples/inbox/tests/test_tickets_rls.py website/src/content/docs/examples/inbox/correlated-rls-subqueries.md
git commit -m "feat(examples): inbox recipe 2 — correlated RLS subqueries"
```

---

### Task 3: Recipe 5 — internal-message-rls

**Files:**
- Modify: `examples/inbox/schema/001_initial.sql` (append buggy RLS policies on `messages`)
- Create: `examples/inbox/schema/006_fix_messages_rls.sql`
- Create: `examples/inbox/tests/test_messages_rls.py`
- Create: `website/src/content/docs/examples/inbox/internal-message-rls.md`

The buggy policy on `messages` gates visibility on parent-ticket access but doesn't gate `is_internal = true`, so customers reading their own ticket see agent-only internal notes.

- [ ] **Step 1: Append the buggy policies to `001_initial.sql`**

Append at the end of the file:

```sql
-- ---------------------------------------------------------------------------
-- Recipe 5 (internal-message-rls) — BUGGY policy on messages
-- ---------------------------------------------------------------------------

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

-- BUG: This policy says "you can read a message iff you can read its
-- parent ticket." That's correct for agents, but customers viewing
-- their own ticket also pass it — and the policy never gates on
-- `is_internal`. Customers read internal agent triage notes meant
-- for staff only.
CREATE POLICY "messages visible with parent ticket" ON messages
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM tickets t
      WHERE t.id = messages.ticket_id
        AND (
          -- Agent/admin path: in the same org as the ticket
          EXISTS (
            SELECT 1 FROM org_members om
            WHERE om.user_id = auth.uid()
              AND om.org_id  = t.org_id
          )
          -- Customer path: a customer can see their ticket's messages.
          -- We simulate "auth.uid() = customer's auth row" via the
          -- ticket's customer_id matching a claim on the JWT.
          OR (auth.jwt() ->> 'customer_id')::uuid = t.customer_id
        )
    )
  );
```

- [ ] **Step 2: Write the failing test to `examples/inbox/tests/test_messages_rls.py`**

```python
"""Recipe 5: internal-message-rls.

The `messages` SELECT policy gates visibility on parent-ticket access
but never checks `is_internal`. Customers viewing their own ticket
read agent-only internal notes.
"""

from __future__ import annotations

import uuid

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.contrib.supabase import as_rls_user

PROOF = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@PROOF
@given(data=st.data())
def test_customer_does_not_see_internal_notes_on_their_own_ticket(
    supabase_proof, data,
) -> None:
    dataset = data.draw(
        supabase_proof.dataset_strategy(
            sizes={
                "organizations": 1,
                "customers": 1,
                "tickets": 1,
                "messages": 3,
            },
            columns={
                "messages.is_internal": st.booleans(),
            },
        ),
    )
    with supabase_proof.client_for_dataset(dataset) as db:
        ticket = dataset["tickets"][0]
        customer = next(
            c for c in dataset["customers"] if c["id"] == ticket["customer_id"]
        )

        # Simulate a logged-in customer: a Supabase auth user with a
        # `customer_id` claim. The pool gives us a real auth.users id;
        # we use it as the "customer's auth user" and pass the
        # customer's row id via extra_claims.
        rows = db.query(
            r"SELECT id::text FROM auth.users WHERE email LIKE %s ESCAPE '\' LIMIT 1",
            r"sqlproof\_%@test.invalid",
        )
        customer_auth_id = rows[0]["id"]

        with as_rls_user(
            db,
            customer_auth_id,
            extra_claims={"customer_id": customer["id"]},
        ):
            visible = db.query(
                "SELECT id, is_internal FROM messages WHERE ticket_id = %s",
                ticket["id"],
            )

        leaked = [m for m in visible if m["is_internal"]]
        assert leaked == [], (
            f"customer leaked internal messages: {leaked}"
        )
```

- [ ] **Step 3: Run the test — expect failure**

Run:
```bash
pytest examples/inbox/tests/test_messages_rls.py -v
```
Expected: FAIL with a counterexample where `is_internal=True` messages are visible to the customer.

- [ ] **Step 4: Write the fix migration to `examples/inbox/schema/006_fix_messages_rls.sql`**

```sql
-- Recipe 5 fix: gate internal messages on the agent/admin path only.
--
-- Customers retain access to non-internal messages on their tickets;
-- internal notes are visible only to org members.

DROP POLICY "messages visible with parent ticket" ON messages;

CREATE POLICY "messages visible with parent ticket" ON messages
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM tickets t
      WHERE t.id = messages.ticket_id
        AND (
          EXISTS (
            SELECT 1 FROM org_members om
            WHERE om.user_id = auth.uid()
              AND om.org_id  = t.org_id
          )
          OR (
            (auth.jwt() ->> 'customer_id')::uuid = t.customer_id
            AND messages.is_internal = false   -- the missing gate
          )
        )
    )
  );
```

- [ ] **Step 5: Apply the fix and re-run the test**

Run:
```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/006_fix_messages_rls.sql
pytest examples/inbox/tests/test_messages_rls.py -v
```
Expected: PASS.

- [ ] **Step 6: Write the doc page to `website/src/content/docs/examples/inbox/internal-message-rls.md`**

```markdown
---
title: Internal messages visible to customers
description: A policy that says "visible with parent ticket" doesn't gate on `is_internal`, leaking agent notes to the customer.
---

## Problem

Agents leave private triage notes on tickets (`is_internal = true`) — "this customer is angry, route to senior support." The customer reads them through the public ticket-detail API.

## The code

```sql
CREATE POLICY "messages visible with parent ticket" ON messages
  FOR SELECT TO authenticated
  USING (
    EXISTS (
      SELECT 1 FROM tickets t
      WHERE t.id = messages.ticket_id
        AND (org_member_check OR customer_owns_ticket_check)
    )
  );
```

## Why review misses it

The mental model is "messages inherit visibility from the parent ticket." That's true for agents — and almost true for customers. The exception (`is_internal`) is invisible in the policy.

## The example test that passes

```python
def test_customer_sees_their_messages(db, ticket, customer_message):
    with as_customer(db, ticket["customer_id"]):
        rows = db.query("SELECT id FROM messages WHERE ticket_id = %s", ticket["id"])
    assert len(rows) == 1
```

Seeds one customer message; doesn't seed an internal note; passes.

## The SqlProof property

```python
dataset = data.draw(supabase_proof.dataset_strategy(
    sizes={"tickets": 1, "messages": 3},
    columns={"messages.is_internal": st.booleans()},
))
with as_rls_user(db, customer_auth_id, extra_claims={"customer_id": customer["id"]}):
    visible = db.query("SELECT id, is_internal FROM messages WHERE ticket_id = %s", ticket["id"])
assert [m for m in visible if m["is_internal"]] == []
```

Notice the `columns={"messages.is_internal": st.booleans()}` override — `is_internal` has a `DEFAULT false`, so the dataset generator omits it; we have to opt in for the test to read it.

## The counterexample

```
Property failed: customer leaked internal messages
Row context: ticket=t1, customer=c1, messages=[m1(is_internal=true)]
```

## The fix

Add the missing gate to the customer branch of the USING clause:

```sql
OR (
  (auth.jwt() ->> 'customer_id')::uuid = t.customer_id
  AND messages.is_internal = false
)
```
```

- [ ] **Step 7: Commit**

```bash
git add examples/inbox/schema/001_initial.sql examples/inbox/schema/006_fix_messages_rls.sql examples/inbox/tests/test_messages_rls.py website/src/content/docs/examples/inbox/internal-message-rls.md
git commit -m "feat(examples): inbox recipe 5 — internal messages RLS leak"
```

---

### Task 4: Recipe 3 — idempotent-status-triggers

**Files:**
- Modify: `examples/inbox/schema/001_initial.sql` (append buggy trigger)
- Create: `examples/inbox/schema/004_fix_resolved_at_trigger.sql`
- Create: `examples/inbox/tests/test_resolved_at_trigger.py`
- Create: `website/src/content/docs/examples/inbox/idempotent-status-triggers.md`

The trigger sets `resolved_at = now()` whenever status is `'resolved'`, including on edits that don't change status — clobbering the original resolution timestamp.

- [ ] **Step 1: Append the buggy trigger to `001_initial.sql`**

Append at the end of the file:

```sql
-- ---------------------------------------------------------------------------
-- Recipe 3 (idempotent-status-triggers) — BUGGY trigger on tickets
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION tg_close_sets_resolved_at()
  RETURNS TRIGGER
  LANGUAGE plpgsql
AS $$
BEGIN
  -- BUG: fires on any update where the NEW status is 'resolved',
  -- including edits that don't change the status. Editing a resolved
  -- ticket's subject bumps `resolved_at`.
  IF NEW.status = 'resolved' THEN
    NEW.resolved_at := now();
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER tg_close_sets_resolved_at
  BEFORE UPDATE ON tickets
  FOR EACH ROW
  EXECUTE FUNCTION tg_close_sets_resolved_at();
```

- [ ] **Step 2: Write the failing test to `examples/inbox/tests/test_resolved_at_trigger.py`**

```python
"""Recipe 3: idempotent-status-triggers.

The trigger sets `resolved_at = now()` whenever NEW.status is
'resolved', including on edits that don't change status. The
property: editing a resolved ticket's subject leaves `resolved_at`
unchanged.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROOF = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@PROOF
@given(
    data=st.data(),
    new_subject=st.text(min_size=1, max_size=80).filter(lambda s: "'" not in s),
)
def test_editing_resolved_ticket_does_not_bump_resolved_at(
    proof, data, new_subject,
) -> None:
    dataset = data.draw(
        proof.dataset_strategy(
            sizes={"organizations": 1, "customers": 1, "tickets": 1},
            columns={
                "tickets.status": st.just("resolved"),
            },
        ),
    )
    with proof.client_for_dataset(dataset) as db:
        ticket_id = dataset["tickets"][0]["id"]

        # Capture the post-insert resolved_at (BEFORE trigger set it).
        before = db.scalar(
            "SELECT resolved_at FROM tickets WHERE id = %s",
            ticket_id,
        )
        assert before is not None, "trigger should have set resolved_at on insert"

        # Edit a non-status field.
        db.execute(
            "UPDATE tickets SET subject = %s WHERE id = %s",
            new_subject, ticket_id,
        )

        after = db.scalar(
            "SELECT resolved_at FROM tickets WHERE id = %s",
            ticket_id,
        )
        assert after == before, (
            f"resolved_at was bumped by a non-status edit: "
            f"before={before}, after={after}"
        )
```

- [ ] **Step 3: Run the test — expect failure**

Run:
```bash
pytest examples/inbox/tests/test_resolved_at_trigger.py -v
```
Expected: FAIL — `after != before` because the trigger fired on the subject edit.

- [ ] **Step 4: Write the fix migration to `examples/inbox/schema/004_fix_resolved_at_trigger.sql`**

```sql
-- Recipe 3 fix: only set resolved_at when the status *transitions*
-- into 'resolved' from something else.

CREATE OR REPLACE FUNCTION tg_close_sets_resolved_at()
  RETURNS TRIGGER
  LANGUAGE plpgsql
AS $$
BEGIN
  IF NEW.status = 'resolved'
     AND (OLD IS NULL OR OLD.status IS DISTINCT FROM 'resolved')
  THEN
    NEW.resolved_at := now();
  END IF;
  RETURN NEW;
END;
$$;
```

- [ ] **Step 5: Apply the fix and re-run the test**

Run:
```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/004_fix_resolved_at_trigger.sql
pytest examples/inbox/tests/test_resolved_at_trigger.py -v
```
Expected: PASS.

- [ ] **Step 6: Write the doc page to `website/src/content/docs/examples/inbox/idempotent-status-triggers.md`**

```markdown
---
title: Triggers that aren't idempotent across no-op updates
description: A status-change trigger that doesn't check the transition fires on every edit.
---

## Problem

A trigger sets `tickets.resolved_at = now()` when a ticket is resolved. A few weeks later, an agent edits the subject of an already-resolved ticket to fix a typo. The `resolved_at` jumps forward by three weeks. SLA reporting silently breaks.

## The code

```sql
CREATE OR REPLACE FUNCTION tg_close_sets_resolved_at() RETURNS TRIGGER AS $$
BEGIN
  IF NEW.status = 'resolved' THEN
    NEW.resolved_at := now();
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
```

## Why review misses it

The trigger reads as "when a ticket is resolved, set the timestamp." Reviewers think in terms of the resolve action, not in terms of every future edit that happens to leave the status set to `'resolved'`.

## The example test that passes

```python
def test_resolving_a_ticket_sets_resolved_at(db, open_ticket):
    db.execute("UPDATE tickets SET status = 'resolved' WHERE id = %s", open_ticket["id"])
    after = db.scalar("SELECT resolved_at FROM tickets WHERE id = %s", open_ticket["id"])
    assert after is not None
```

Tests the *transition*; doesn't test the no-op update.

## The SqlProof property

```python
@given(new_subject=st.text(min_size=1, max_size=80))
def test_editing_resolved_ticket_does_not_bump_resolved_at(proof, data, new_subject):
    dataset = data.draw(proof.dataset_strategy(
        sizes={"tickets": 1},
        columns={"tickets.status": st.just("resolved")},
    ))
    with proof.client_for_dataset(dataset) as db:
        before = db.scalar("SELECT resolved_at FROM tickets WHERE id = %s", t_id)
        db.execute("UPDATE tickets SET subject = %s WHERE id = %s", new_subject, t_id)
        after  = db.scalar("SELECT resolved_at FROM tickets WHERE id = %s", t_id)
        assert after == before
```

The phrasing is "applying the update *does not change* `resolved_at`" — an idempotency property: doing the operation N times should equal doing it once (and zero times changes nothing).

## The counterexample

```
Property failed: resolved_at was bumped by a non-status edit
before=2026-06-03 10:00:00+00, after=2026-06-03 10:00:00.000001+00
```

## The fix

Check that the status *transitioned* into `'resolved'`:

```sql
IF NEW.status = 'resolved'
   AND (OLD IS NULL OR OLD.status IS DISTINCT FROM 'resolved')
THEN
  NEW.resolved_at := now();
END IF;
```
```

- [ ] **Step 7: Commit**

```bash
git add examples/inbox/schema/001_initial.sql examples/inbox/schema/004_fix_resolved_at_trigger.sql examples/inbox/tests/test_resolved_at_trigger.py website/src/content/docs/examples/inbox/idempotent-status-triggers.md
git commit -m "feat(examples): inbox recipe 3 — non-idempotent resolved_at trigger"
```

---

### Task 5: Recipe 4 — outer-joins-and-where

**Files:**
- Modify: `examples/inbox/schema/001_initial.sql` (append buggy `organization_dashboard` RPC)
- Create: `examples/inbox/schema/005_fix_dashboard.sql`
- Create: `examples/inbox/tests/test_dashboard.py`
- Create: `website/src/content/docs/examples/inbox/outer-joins-and-where.md`

The dashboard function uses a LEFT JOIN to enumerate every status, but a WHERE clause on the right-side table collapses it to an INNER JOIN, dropping status buckets with zero tickets.

- [ ] **Step 1: Append the buggy RPC to `001_initial.sql`**

Append at the end of the file:

```sql
-- ---------------------------------------------------------------------------
-- Recipe 4 (outer-joins-and-where) — BUGGY dashboard RPC
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION organization_dashboard(p_org_id UUID)
  RETURNS TABLE (status ticket_status, count BIGINT)
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  -- BUG: the LEFT JOIN intends "show every status, even zero ones",
  -- but `WHERE t.org_id = p_org_id` collapses it to INNER, dropping
  -- the zero-bucket rows. Dashboards silently lose "pending: 0",
  -- "reopened: 0", etc.
  SELECT s.status, count(t.id)
  FROM unnest(enum_range(NULL::ticket_status)) AS s(status)
  LEFT JOIN tickets t ON t.status = s.status
  WHERE t.org_id = p_org_id
  GROUP BY s.status;
$$;

GRANT EXECUTE ON FUNCTION organization_dashboard(UUID) TO authenticated;
```

- [ ] **Step 2: Write the failing test to `examples/inbox/tests/test_dashboard.py`**

```python
"""Recipe 4: outer-joins-and-where.

`organization_dashboard` should return one row per ticket_status enum
value. The buggy implementation drops zero-bucket rows because a WHERE
clause on the right side of the LEFT JOIN collapses it to INNER.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROOF = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

ALL_STATUSES = {"open", "pending", "resolved", "reopened"}


@PROOF
@given(data=st.data())
def test_dashboard_returns_every_status_bucket(proof, data) -> None:
    dataset = data.draw(
        proof.dataset_strategy(
            sizes={
                "organizations": 1,
                "customers": 1,
                "tickets": st.integers(min_value=0, max_value=5),
            },
        ),
    )
    with proof.client_for_dataset(dataset) as db:
        org_id = dataset["organizations"][0]["id"]
        rows = db.query(
            "SELECT status, count FROM organization_dashboard(%s)",
            org_id,
        )
        present = {row["status"] for row in rows}
        assert present == ALL_STATUSES, (
            f"dashboard dropped status buckets: missing {ALL_STATUSES - present}"
        )


@PROOF
@given(data=st.data())
def test_dashboard_counts_sum_to_org_ticket_total(proof, data) -> None:
    dataset = data.draw(
        proof.dataset_strategy(
            sizes={"organizations": 1, "customers": 1, "tickets": 5},
        ),
    )
    with proof.client_for_dataset(dataset) as db:
        org_id = dataset["organizations"][0]["id"]
        rows = db.query(
            "SELECT count FROM organization_dashboard(%s)",
            org_id,
        )
        dashboard_total = sum(row["count"] for row in rows)
        actual_total = db.scalar(
            "SELECT count(*) FROM tickets WHERE org_id = %s",
            org_id,
        )
        assert dashboard_total == actual_total
```

- [ ] **Step 3: Run the tests — expect failure**

Run:
```bash
pytest examples/inbox/tests/test_dashboard.py -v
```
Expected: FAIL with a counterexample where the dashboard returns fewer than 4 rows for an org missing one or more status buckets.

- [ ] **Step 4: Write the fix migration to `examples/inbox/schema/005_fix_dashboard.sql`**

```sql
-- Recipe 4 fix: move the org_id filter into the JOIN condition so the
-- LEFT JOIN really is a LEFT JOIN.

CREATE OR REPLACE FUNCTION organization_dashboard(p_org_id UUID)
  RETURNS TABLE (status ticket_status, count BIGINT)
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  SELECT s.status, count(t.id)
  FROM unnest(enum_range(NULL::ticket_status)) AS s(status)
  LEFT JOIN tickets t
    ON t.status = s.status
   AND t.org_id = p_org_id
  GROUP BY s.status;
$$;
```

- [ ] **Step 5: Apply the fix and re-run the tests**

Run:
```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/005_fix_dashboard.sql
pytest examples/inbox/tests/test_dashboard.py -v
```
Expected: 2 passed.

- [ ] **Step 6: Write the doc page to `website/src/content/docs/examples/inbox/outer-joins-and-where.md`**

```markdown
---
title: LEFT JOIN collapsed to INNER by a WHERE clause
description: A dashboard query intends to enumerate every status bucket; a WHERE on the right side silently drops zero-count rows.
---

## Problem

The Org Admin dashboard shows ticket counts by status. The chart renders fine for active orgs. When a new org is created — or an existing org has no `reopened` tickets — the corresponding bucket simply doesn't appear. The frontend assumes a complete enum and renders `undefined`.

## The code

```sql
CREATE FUNCTION organization_dashboard(p_org_id UUID) RETURNS TABLE (status ticket_status, count BIGINT) AS $$
  SELECT s.status, count(t.id)
  FROM unnest(enum_range(NULL::ticket_status)) AS s(status)
  LEFT JOIN tickets t ON t.status = s.status
  WHERE t.org_id = p_org_id
  GROUP BY s.status;
$$;
```

## Why review misses it

Engineers know `LEFT JOIN ... WHERE right_side = X` is risky in the abstract — but here the `WHERE` reads as the tenant filter. It scans as "show every status; filter by org." The "filter by org" reads as a constraint on the result, not as a transformation of the join.

## The example test that passes

```python
def test_dashboard_returns_counts(db, org):
    seed_tickets(db, org["id"], statuses=["open", "pending", "resolved", "reopened"])
    rows = db.query("SELECT status, count FROM organization_dashboard(%s)", org["id"])
    assert len(rows) == 4
```

Seeds one ticket in each status. All four buckets present. Test green. The bug only fires when a bucket is empty.

## The SqlProof property

```python
@given(data=st.data())
def test_dashboard_returns_every_status_bucket(proof, data):
    dataset = data.draw(proof.dataset_strategy(
        sizes={"organizations": 1, "tickets": st.integers(min_value=0, max_value=5)},
    ))
    with proof.client_for_dataset(dataset) as db:
        rows = db.query("SELECT status FROM organization_dashboard(%s)", org_id)
        assert {r["status"] for r in rows} == {"open", "pending", "resolved", "reopened"}
```

A second property asserts `sum(counts) == count(*)` — an even tighter aggregation invariant.

## The counterexample

```
Property failed: dashboard dropped status buckets: missing {'reopened'}
Dataset: {"organizations": 1, "tickets": 3} — none with status='reopened'
```

## The fix

Move the org filter into the join condition:

```sql
LEFT JOIN tickets t
  ON t.status = s.status
 AND t.org_id = p_org_id
```

Now the LEFT JOIN really is a LEFT JOIN, regardless of how many tickets each bucket contains.
```

- [ ] **Step 7: Commit**

```bash
git add examples/inbox/schema/001_initial.sql examples/inbox/schema/005_fix_dashboard.sql examples/inbox/tests/test_dashboard.py website/src/content/docs/examples/inbox/outer-joins-and-where.md
git commit -m "feat(examples): inbox recipe 4 — outer-join-vs-where dashboard"
```

---

### Task 6: Recipe 9 — mass-assignment-without-with-check

**Files:**
- Modify: `examples/inbox/schema/001_initial.sql` (append buggy UPDATE policy on `org_members`)
- Create: `examples/inbox/schema/011_fix_org_members_with_check.sql`
- Create: `examples/inbox/tests/test_org_members_mass_assignment.py`
- Create: `website/src/content/docs/examples/inbox/mass-assignment-without-with-check.md`

The buggy UPDATE policy on `org_members` allows members to edit their own row but doesn't constrain *which columns* — they self-promote by changing `role` to `'admin'`.

- [ ] **Step 1: Append the buggy UPDATE policy to `001_initial.sql`**

Append at the end of the file:

```sql
-- ---------------------------------------------------------------------------
-- Recipe 9 (mass-assignment-without-with-check) — BUGGY UPDATE policy
-- ---------------------------------------------------------------------------

-- BUG: missing WITH CHECK. Members can `UPDATE org_members SET role
-- = 'admin' WHERE user_id = auth.uid()` and self-promote. The USING
-- clause restricts *which rows* they can touch; without WITH CHECK,
-- nothing restricts *what they can change about that row*.
CREATE POLICY "members manage their own row" ON org_members
  FOR UPDATE TO authenticated
  USING (org_members.user_id = auth.uid());
```

- [ ] **Step 2: Write the failing test to `examples/inbox/tests/test_org_members_mass_assignment.py`**

```python
"""Recipe 9: mass-assignment-without-with-check.

The "members manage their own row" UPDATE policy on `org_members`
restricts *which row* a member can touch (`USING ... user_id =
auth.uid()`) but doesn't constrain *what columns* they can change.
A viewer self-promotes to admin.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.contrib.supabase import as_rls_user

PROOF = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@PROOF
@given(data=st.data())
def test_viewer_cannot_self_promote_to_admin(supabase_proof, data) -> None:
    dataset = data.draw(
        supabase_proof.dataset_strategy(
            sizes={"organizations": 1, "org_members": 1},
            columns={
                "org_members.role": st.just("viewer"),
            },
        ),
    )
    with supabase_proof.client_for_dataset(dataset) as db:
        member = dataset["org_members"][0]

        with as_rls_user(db, member["user_id"]):
            db.execute(
                "UPDATE org_members SET role = 'admin' "
                "WHERE org_id = %s AND user_id = %s",
                member["org_id"], member["user_id"],
            )

        # Read back as a privileged role (test connection is postgres,
        # so RLS is bypassed for the verification read).
        role_after = db.scalar(
            "SELECT role FROM org_members WHERE org_id = %s AND user_id = %s",
            member["org_id"], member["user_id"],
        )
        assert role_after == "viewer", (
            f"viewer self-promoted to {role_after!r}"
        )
```

- [ ] **Step 3: Run the test — expect failure**

Run:
```bash
pytest examples/inbox/tests/test_org_members_mass_assignment.py -v
```
Expected: FAIL — `role_after == 'admin'`.

- [ ] **Step 4: Write the fix migration to `examples/inbox/schema/011_fix_org_members_with_check.sql`**

```sql
-- Recipe 9 fix: add a WITH CHECK clause that pins the role.
--
-- WITH CHECK is evaluated against the *new* row state; pinning
-- `role` to its prior value prevents mass-assignment without
-- requiring a separate column-level grant.

DROP POLICY "members manage their own row" ON org_members;

CREATE POLICY "members manage their own row" ON org_members
  FOR UPDATE TO authenticated
  USING      (org_members.user_id = auth.uid())
  WITH CHECK (
    org_members.user_id = auth.uid()
    AND org_members.role = (
      SELECT role FROM org_members om2
      WHERE om2.org_id = org_members.org_id
        AND om2.user_id = auth.uid()
    )
  );
```

- [ ] **Step 5: Apply the fix and re-run the test**

Run:
```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/011_fix_org_members_with_check.sql
pytest examples/inbox/tests/test_org_members_mass_assignment.py -v
```
Expected: PASS — `role_after == 'viewer'`.

- [ ] **Step 6: Write the doc page to `website/src/content/docs/examples/inbox/mass-assignment-without-with-check.md`**

```markdown
---
title: Mass assignment — UPDATE policies without WITH CHECK
description: A member with permission to edit their own row can change *any column* of that row, including their role.
---

## Problem

You ship a policy: "members can edit their own row in `org_members`." A viewer issues `UPDATE org_members SET role = 'admin' WHERE user_id = auth.uid()` and silently becomes an admin.

## The code

```sql
CREATE POLICY "members manage their own row" ON org_members
  FOR UPDATE TO authenticated
  USING (org_members.user_id = auth.uid());
```

## Why review misses it

`USING (user_id = auth.uid())` reads as "members can only touch *their own* row" — and that's true. The blind spot is between "which rows can they touch" (USING) and "what state can the row end up in" (WITH CHECK). Reviewers conflate the two.

## The example test that passes

```python
def test_member_can_update_their_display_field(db, viewer_member):
    with as_rls_user(db, viewer_member["user_id"]):
        db.execute("UPDATE org_members SET role = role WHERE user_id = %s", viewer_member["user_id"])
    # No exception raised — policy permits the update.
```

The test confirms members can update — but doesn't check *what they can change*.

## The SqlProof property

```python
@given(data=st.data())
def test_viewer_cannot_self_promote_to_admin(supabase_proof, data):
    dataset = data.draw(supabase_proof.dataset_strategy(
        sizes={"org_members": 1},
        columns={"org_members.role": st.just("viewer")},
    ))
    with supabase_proof.client_for_dataset(dataset) as db:
        with as_rls_user(db, member["user_id"]):
            db.execute("UPDATE org_members SET role = 'admin' WHERE user_id = %s", member["user_id"])
        role_after = db.scalar("SELECT role FROM org_members WHERE user_id = %s", member["user_id"])
        assert role_after == "viewer"
```

**The key idea**: assert the *post-state*, not the return value. The UPDATE doesn't raise an error; the policy quietly applies; the only evidence of the bug is in the row.

## The counterexample

```
Property failed: viewer self-promoted to 'admin'
Dataset: {"org_members": [{role: "viewer", ...}]}
```

## The fix

Add `WITH CHECK` that pins `role` to its existing value:

```sql
WITH CHECK (
  user_id = auth.uid()
  AND role = (SELECT role FROM org_members WHERE ...)
)
```

See also [Missing DELETE policy](missing-delete-policy) — the sibling write-side RLS bug.
```

- [ ] **Step 7: Commit**

```bash
git add examples/inbox/schema/001_initial.sql examples/inbox/schema/011_fix_org_members_with_check.sql examples/inbox/tests/test_org_members_mass_assignment.py website/src/content/docs/examples/inbox/mass-assignment-without-with-check.md
git commit -m "feat(examples): inbox recipe 9 — mass assignment without WITH CHECK"
```

---

### Task 7: Recipe 10 — missing-delete-policy

**Files:**
- Modify: `examples/inbox/schema/001_initial.sql` (append overly-permissive DELETE policy)
- Create: `examples/inbox/schema/012_add_org_members_delete_policy.sql`
- Create: `examples/inbox/tests/test_org_members_delete_policy.py`
- Create: `website/src/content/docs/examples/inbox/missing-delete-policy.md`

**Note on spec divergence:** The spec describes this recipe as "DELETE policy doesn't exist — PostgREST permits the DELETE." That bug class only manifests at the PostgREST layer; in raw Postgres (which sqlproof tests through psycopg), RLS-enabled + absent DELETE policy = deny-all, which can't be reproduced as a bug. This task instead implements the closely-related "overly permissive DELETE policy with `USING (true)`" — same write-side blind spot for reviewers, but reproducible end-to-end through Postgres. The doc page and recipe slug retain the `missing-delete-policy` framing to match the spec; the buggy SQL ships an over-permissive policy and the fix tightens it. Surface this divergence to the spec author if a closer match to the PostgREST framing is required.

- [ ] **Step 1: Append the buggy DELETE policy to `001_initial.sql`**

Append at the end of the file:

```sql
-- ---------------------------------------------------------------------------
-- Recipe 10 (missing-delete-policy) — overly permissive DELETE policy
-- ---------------------------------------------------------------------------

-- BUG: shipped as "any authenticated user can delete their own row"
-- but the USING clause doesn't restrict *who* the deleted row belongs
-- to — a viewer can issue a DELETE that removes an admin from the
-- org. (The intent was probably user_id = auth.uid(); the shipped
-- version forgot the constraint entirely.)
CREATE POLICY "members manage their own row delete" ON org_members
  FOR DELETE TO authenticated
  USING (true);
```

- [ ] **Step 2: Write the failing test to `examples/inbox/tests/test_org_members_delete_policy.py`**

```python
"""Recipe 10: missing-delete-policy.

The DELETE policy on `org_members` was shipped with `USING (true)`,
meaning any authenticated user can delete any row. A viewer in org A
can eject an admin from org A — or, worse, eject members from orgs
they aren't part of at all.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.contrib.supabase import as_rls_user

PROOF = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@PROOF
@given(data=st.data())
def test_viewer_cannot_delete_admin_in_same_org(supabase_proof, data) -> None:
    dataset = data.draw(
        supabase_proof.dataset_strategy(
            sizes={"organizations": 1, "org_members": 2},
            columns={
                "org_members.role": st.sampled_from(["viewer", "admin"]),
            },
        ),
    )
    with supabase_proof.client_for_dataset(dataset) as db:
        members = dataset["org_members"]
        viewers = [m for m in members if m["role"] == "viewer"]
        admins  = [m for m in members if m["role"] == "admin"]
        if not viewers or not admins:
            return  # let Hypothesis re-draw
        viewer, admin = viewers[0], admins[0]

        with as_rls_user(db, viewer["user_id"]):
            db.execute(
                "DELETE FROM org_members WHERE org_id = %s AND user_id = %s",
                admin["org_id"], admin["user_id"],
            )

        still_present = db.scalar(
            "SELECT count(*) FROM org_members WHERE org_id = %s AND user_id = %s",
            admin["org_id"], admin["user_id"],
        )
        assert still_present == 1, (
            f"viewer deleted admin's membership; rows remaining: {still_present}"
        )
```

- [ ] **Step 3: Run the test — expect failure**

Run:
```bash
pytest examples/inbox/tests/test_org_members_delete_policy.py -v
```
Expected: FAIL — `still_present == 0`.

- [ ] **Step 4: Write the fix migration to `examples/inbox/schema/012_add_org_members_delete_policy.sql`**

```sql
-- Recipe 10 fix: restrict DELETE to (a) the caller deleting their own
-- row, or (b) an admin in the same org deleting another member.

DROP POLICY "members manage their own row delete" ON org_members;

CREATE POLICY "members manage their own row delete" ON org_members
  FOR DELETE TO authenticated
  USING (
    org_members.user_id = auth.uid()
    OR EXISTS (
      SELECT 1 FROM org_members om
      WHERE om.user_id = auth.uid()
        AND om.org_id  = org_members.org_id
        AND om.role    = 'admin'
    )
  );
```

- [ ] **Step 5: Apply the fix and re-run the test**

Run:
```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/012_add_org_members_delete_policy.sql
pytest examples/inbox/tests/test_org_members_delete_policy.py -v
```
Expected: PASS.

- [ ] **Step 6: Write the doc page to `website/src/content/docs/examples/inbox/missing-delete-policy.md`**

```markdown
---
title: Overly permissive DELETE policy
description: A `USING (true)` DELETE policy lets any authenticated user delete any row — including admins from orgs they aren't part of.
---

## Problem

A viewer issues `DELETE FROM org_members WHERE org_id = 'X' AND user_id = '<admin>'` and silently ejects an admin from an org they don't belong to.

## The code

```sql
CREATE POLICY "members manage their own row delete" ON org_members
  FOR DELETE TO authenticated
  USING (true);
```

## Why review misses it

The reviewer reads the SELECT and UPDATE policies (which are correctly constrained) and assumes consistency. The DELETE policy was added later "to fix a flaky test" and quietly shipped without the same constraints.

## The example test that passes

```python
def test_member_can_remove_themselves(db, admin_member):
    with as_rls_user(db, admin_member["user_id"]):
        db.execute("DELETE FROM org_members WHERE user_id = %s", admin_member["user_id"])
    remaining = db.scalar("SELECT count(*) FROM org_members WHERE user_id = %s", admin_member["user_id"])
    assert remaining == 0
```

Confirms the happy path. Doesn't probe whether the policy *should have stopped* a wider class of deletes.

## The SqlProof property

```python
dataset = data.draw(supabase_proof.dataset_strategy(
    sizes={"org_members": 2},
    columns={"org_members.role": st.sampled_from(["viewer", "admin"])},
))
with as_rls_user(db, viewer["user_id"]):
    db.execute("DELETE FROM org_members WHERE org_id = %s AND user_id = %s", admin["org_id"], admin["user_id"])
still_present = db.scalar("SELECT count(*) FROM org_members WHERE ...")
assert still_present == 1
```

Same idea as recipe 9: assert the *post-state* of a malicious write, not the return value.

## The counterexample

```
Property failed: viewer deleted admin's membership
Dataset: org_members=[viewer u1, admin u2]
```

## The fix

Add the constraints that should have shipped with the original policy:

```sql
USING (
  user_id = auth.uid()
  OR EXISTS (SELECT 1 FROM org_members om
             WHERE om.user_id = auth.uid()
               AND om.org_id  = org_members.org_id
               AND om.role    = 'admin')
)
```

See also [Mass assignment without WITH CHECK](mass-assignment-without-with-check) — the same blind spot, different operation.
```

- [ ] **Step 7: Commit**

```bash
git add examples/inbox/schema/001_initial.sql examples/inbox/schema/012_add_org_members_delete_policy.sql examples/inbox/tests/test_org_members_delete_policy.py website/src/content/docs/examples/inbox/missing-delete-policy.md
git commit -m "feat(examples): inbox recipe 10 — overly permissive DELETE policy"
```

---

### Task 8: Recipe 8 — stateful-ticket-lifecycle

**Files:**
- Modify: `examples/inbox/schema/001_initial.sql` (append buggy `reopen_ticket` RPC)
- Create: `examples/inbox/schema/010_fix_reopen_ticket.sql`
- Create: `examples/inbox/tests/test_ticket_lifecycle.py`
- Create: `website/src/content/docs/examples/inbox/stateful-ticket-lifecycle.md`

The `reopen_ticket` RPC sets `status = 'reopened'` but forgets to clear `resolved_at`. A single-shot property test misses it because the invariant holds at every individual state — only the transition resolve→reopen leaves a stale `resolved_at`.

- [ ] **Step 1: Append the buggy RPC to `001_initial.sql`**

Append at the end of the file:

```sql
-- ---------------------------------------------------------------------------
-- Recipe 8 (stateful-ticket-lifecycle) — BUGGY reopen RPC
-- ---------------------------------------------------------------------------

-- BUG: sets status to 'reopened' but forgets to clear resolved_at.
-- The invariant "non-resolved status -> resolved_at IS NULL" holds
-- at every isolated state — only the transition resolve->reopen
-- leaves a stale value behind.
CREATE OR REPLACE FUNCTION reopen_ticket(p_ticket_id UUID)
  RETURNS VOID
  LANGUAGE sql
  SECURITY DEFINER
  SET search_path = public
AS $$
  UPDATE tickets SET status = 'reopened' WHERE id = p_ticket_id;
$$;

GRANT EXECUTE ON FUNCTION reopen_ticket(UUID) TO authenticated;
```

- [ ] **Step 2: Write the failing state-machine test to `examples/inbox/tests/test_ticket_lifecycle.py`**

```python
"""Recipe 8: stateful-ticket-lifecycle.

A state machine that cycles a ticket through resolve <-> reopen and
asserts that `resolved_at` is NULL whenever the status is not
'resolved'. The bug surfaces only after the sequence
{resolve -> reopen}: status becomes 'reopened' but resolved_at
remains stale.
"""

from __future__ import annotations

from hypothesis.stateful import invariant, rule

from sqlproof.testing import SqlProofStateMachine


class TicketLifecycleMachine(SqlProofStateMachine):
    sizes = {"organizations": 1, "customers": 1, "tickets": 1}

    def on_setup(self) -> None:
        self.ticket_id = self.dataset["tickets"][0]["id"]
        # Force a known initial state: open.
        self.db.execute(
            "UPDATE tickets SET status = 'open', resolved_at = NULL "
            "WHERE id = %s",
            self.ticket_id,
        )

    @rule()
    def resolve(self) -> None:
        self.db.execute(
            "UPDATE tickets SET status = 'resolved' WHERE id = %s",
            self.ticket_id,
        )

    @rule()
    def reopen(self) -> None:
        self.db.execute("SELECT reopen_ticket(%s)", self.ticket_id)

    @rule()
    def edit_subject(self) -> None:
        self.db.execute(
            "UPDATE tickets SET subject = subject || '.' WHERE id = %s",
            self.ticket_id,
        )

    @invariant()
    def non_resolved_status_means_resolved_at_is_null(self) -> None:
        row = self.db.query(
            "SELECT status, resolved_at FROM tickets WHERE id = %s",
            self.ticket_id,
        )[0]
        if row["status"] != "resolved":
            assert row["resolved_at"] is None, (
                f"stale resolved_at: status={row['status']!r}, "
                f"resolved_at={row['resolved_at']}"
            )


def test_ticket_lifecycle_invariant(proof) -> None:
    proof.run_state_machine(TicketLifecycleMachine)
```

- [ ] **Step 3: Run the test — expect failure**

Run:
```bash
pytest examples/inbox/tests/test_ticket_lifecycle.py -v
```
Expected: FAIL — Hypothesis finds a sequence `resolve()` then `reopen()` and the invariant catches a non-NULL `resolved_at`.

- [ ] **Step 4: Write the fix migration to `examples/inbox/schema/010_fix_reopen_ticket.sql`**

```sql
-- Recipe 8 fix: clear resolved_at when reopening.

CREATE OR REPLACE FUNCTION reopen_ticket(p_ticket_id UUID)
  RETURNS VOID
  LANGUAGE sql
  SECURITY DEFINER
  SET search_path = public
AS $$
  UPDATE tickets
     SET status      = 'reopened',
         resolved_at = NULL
   WHERE id = p_ticket_id;
$$;
```

- [ ] **Step 5: Apply the fix and re-run the test**

Run:
```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/010_fix_reopen_ticket.sql
pytest examples/inbox/tests/test_ticket_lifecycle.py -v
```
Expected: PASS.

- [ ] **Step 6: Write the doc page to `website/src/content/docs/examples/inbox/stateful-ticket-lifecycle.md`**

```markdown
---
title: Stale resolved_at after resolve→reopen
description: A sequence-dependent bug a single-shot property test cannot reach.
---

## Problem

A customer reopens a ticket. The status field flips to `reopened` but the `resolved_at` timestamp stays set to the original resolution time. SLA reporting now thinks the ticket is both reopened *and* resolved. A snapshot test of an open or resolved ticket passes; only the *transition* exposes the inconsistency.

## The code

```sql
CREATE FUNCTION reopen_ticket(p_ticket_id UUID) RETURNS VOID AS $$
  UPDATE tickets SET status = 'reopened' WHERE id = p_ticket_id;
$$ LANGUAGE sql;
```

## Why review misses it

You read the function and confirm "yes, it reopens." You don't look at every column the ticket has and ask "what stale state could be left behind?" The bug is in what the function *doesn't* update.

## The example test that passes

```python
def test_reopen_sets_status(db, resolved_ticket):
    db.scalar("SELECT reopen_ticket(%s)", resolved_ticket["id"])
    status = db.scalar("SELECT status FROM tickets WHERE id = %s", resolved_ticket["id"])
    assert status == "reopened"
```

Tests the one thing the function *does*. Misses the thing it *should also have done*.

## The SqlProof property

A `@given` test asserting `if status != 'resolved' then resolved_at IS NULL` against a freshly-generated ticket would pass — because every individual ticket state respects the invariant. The bug requires a *sequence*.

```python
class TicketLifecycleMachine(SqlProofStateMachine):
    @rule()
    def resolve(self): self.db.execute("UPDATE tickets SET status='resolved' WHERE id=%s", self.t)
    @rule()
    def reopen(self):  self.db.execute("SELECT reopen_ticket(%s)", self.t)

    @invariant()
    def consistent(self):
        row = self.db.query("SELECT status, resolved_at FROM tickets WHERE id=%s", self.t)[0]
        if row["status"] != "resolved":
            assert row["resolved_at"] is None
```

## The counterexample

```
Property failed: stale resolved_at
Sequence: resolve(), reopen()
Final state: status='reopened', resolved_at=2026-06-03 10:00:00+00
```

## The fix

```sql
UPDATE tickets SET status = 'reopened', resolved_at = NULL WHERE id = p_ticket_id;
```

## When to reach for state machines

A `@given` property test would have generated thousands of individual tickets and asserted the invariant on each. Every individual ticket would have satisfied it. The bug is in the *transition* between two valid states.

Rule of thumb:
- **Single update** changes a row from invalid to invalid? → `@given` catches it on the first example.
- **Sequence** of updates leaves a row in a logically-impossible state? → state machine.

State machines are slower (each example replays N rules), so don't reach for them when a single-shot property would do — see [recipe 3](idempotent-status-triggers) for the single-shot variant.
```

- [ ] **Step 7: Commit**

```bash
git add examples/inbox/schema/001_initial.sql examples/inbox/schema/010_fix_reopen_ticket.sql examples/inbox/tests/test_ticket_lifecycle.py website/src/content/docs/examples/inbox/stateful-ticket-lifecycle.md
git commit -m "feat(examples): inbox recipe 8 — stateful ticket lifecycle"
```

---

### Task 9: Recipe 7 — equivalent-query-optimization

**Files:**
- Modify: `examples/inbox/schema/001_initial.sql` (append v1 of `agent_workload_summary` — the canonical correct-but-slow version)
- Create: `examples/inbox/schema/008_add_workload_summary_v2.sql` (adds the "optimized" v2 — subtly diverges)
- Create: `examples/inbox/schema/009_fix_workload_summary_v2_nulls.sql` (COALESCE fix to v2)
- Create: `examples/inbox/tests/test_workload_summary.py`
- Create: `website/src/content/docs/examples/inbox/equivalent-query-optimization.md`

- [ ] **Step 1: Append v1 of `agent_workload_summary` to `001_initial.sql`**

Append at the end of the file:

```sql
-- ---------------------------------------------------------------------------
-- Recipe 7 (equivalent-query-optimization) — v1 reference implementation
-- ---------------------------------------------------------------------------

-- The canonical "slow but correct" version: one correlated subquery
-- per metric. v2 (in 008_add_workload_summary_v2.sql) collapses these
-- to a single LEFT JOIN with FILTER, which should be equivalent — but
-- isn't, until 009_fix_workload_summary_v2_nulls.sql applies COALESCE
-- to match v1's "0 instead of NULL" contract.

CREATE OR REPLACE FUNCTION agent_workload_summary_v1(p_org_id UUID)
  RETURNS TABLE (
    user_id          UUID,
    open_count       BIGINT,
    pending_count    BIGINT,
    sla_breach_count BIGINT
  )
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  SELECT
    m.user_id,
    (SELECT count(*) FROM tickets t
       WHERE t.assigned_agent_id = m.user_id
         AND t.status = 'open')    AS open_count,
    (SELECT count(*) FROM tickets t
       WHERE t.assigned_agent_id = m.user_id
         AND t.status = 'pending') AS pending_count,
    (SELECT count(*) FROM tickets t
       WHERE t.assigned_agent_id = m.user_id
         AND t.sla_due_at IS NOT NULL
         AND t.resolved_at IS NOT NULL
         AND t.sla_due_at < t.resolved_at) AS sla_breach_count
  FROM org_members m
  WHERE m.org_id = p_org_id
    AND m.role   = 'agent';
$$;

GRANT EXECUTE ON FUNCTION agent_workload_summary_v1(UUID) TO authenticated;
```

- [ ] **Step 2: Write the v2 migration to `examples/inbox/schema/008_add_workload_summary_v2.sql`**

```sql
-- Recipe 7: ships the "optimized" v2 — a single scan with FILTER
-- aggregations. Should be equivalent to v1. Hypothesis will find
-- that it isn't: agents with zero tickets produce NULL counts in v2
-- where v1 returns 0.

CREATE OR REPLACE FUNCTION agent_workload_summary_v2(p_org_id UUID)
  RETURNS TABLE (
    user_id          UUID,
    open_count       BIGINT,
    pending_count    BIGINT,
    sla_breach_count BIGINT
  )
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  SELECT
    m.user_id,
    count(*) FILTER (WHERE t.status = 'open')    AS open_count,
    count(*) FILTER (WHERE t.status = 'pending') AS pending_count,
    count(*) FILTER (
      WHERE t.sla_due_at IS NOT NULL
        AND t.resolved_at IS NOT NULL
        AND t.sla_due_at < t.resolved_at
    ) AS sla_breach_count
  FROM org_members m
  LEFT JOIN tickets t ON t.assigned_agent_id = m.user_id
  WHERE m.org_id = p_org_id
    AND m.role   = 'agent'
  GROUP BY m.user_id;
$$;

GRANT EXECUTE ON FUNCTION agent_workload_summary_v2(UUID) TO authenticated;
```

- [ ] **Step 3: Write the failing equivalence test to `examples/inbox/tests/test_workload_summary.py`**

```python
"""Recipe 7: equivalent-query-optimization.

Equivalence property: for any generated dataset,
`agent_workload_summary_v1(org)` and `agent_workload_summary_v2(org)`
return the same multiset of rows. The v2 candidate is added by
migration 008; this test skips cleanly if it isn't loaded yet.

NOTE: this is a "scaffolding" test — short-lived. Real-world usage:
write it during the refactor PR, keep it green-gating CI through the
deprecation window, delete it once v1 is dropped. See the recipe page
for the full lifecycle.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PROOF = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def _v2_loaded(db) -> bool:
    return (
        db.scalar(
            "SELECT to_regprocedure('public.agent_workload_summary_v2(uuid)') "
            "IS NOT NULL",
        )
        is True
    )


def _sorted_rows(rows: list[dict]) -> list[tuple]:
    return sorted(
        (
            r["user_id"],
            r["open_count"],
            r["pending_count"],
            r["sla_breach_count"],
        )
        for r in rows
    )


@PROOF
@given(data=st.data())
def test_workload_summary_v1_equivalent_to_v2(proof, data) -> None:
    dataset = data.draw(
        proof.dataset_strategy(
            sizes={
                "organizations": 1,
                "customers": 1,
                "org_members": 3,
                "tickets": st.integers(min_value=0, max_value=10),
            },
            columns={
                "org_members.role": st.just("agent"),
            },
        ),
    )
    with proof.client_for_dataset(dataset) as db:
        if not _v2_loaded(db):
            pytest.skip("apply 008_add_workload_summary_v2.sql first")

        org_id = dataset["organizations"][0]["id"]
        v1 = _sorted_rows(
            db.query(
                "SELECT * FROM agent_workload_summary_v1(%s)",
                org_id,
            ),
        )
        v2 = _sorted_rows(
            db.query(
                "SELECT * FROM agent_workload_summary_v2(%s)",
                org_id,
            ),
        )
        assert v1 == v2, f"v1 != v2:\n  v1={v1}\n  v2={v2}"
```

- [ ] **Step 4: Run the test pre-v2 — expect skip**

Run:
```bash
pytest examples/inbox/tests/test_workload_summary.py -v
```
Expected: 1 skipped — "apply 008_add_workload_summary_v2.sql first".

- [ ] **Step 5: Apply v2 and re-run — expect failure**

Run:
```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/008_add_workload_summary_v2.sql
pytest examples/inbox/tests/test_workload_summary.py -v
```
Expected: FAIL — Hypothesis finds an agent with zero tickets where v1 returns `(0, 0, 0)` and v2 returns `(NULL, NULL, NULL)`.

- [ ] **Step 6: Write the fix migration to `examples/inbox/schema/009_fix_workload_summary_v2_nulls.sql`**

```sql
-- Recipe 7 fix: COALESCE every aggregate in v2 so that agents with
-- zero matching tickets produce 0 instead of NULL — matching v1's
-- contract.

CREATE OR REPLACE FUNCTION agent_workload_summary_v2(p_org_id UUID)
  RETURNS TABLE (
    user_id          UUID,
    open_count       BIGINT,
    pending_count    BIGINT,
    sla_breach_count BIGINT
  )
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  SELECT
    m.user_id,
    coalesce(count(*) FILTER (WHERE t.status = 'open'),    0) AS open_count,
    coalesce(count(*) FILTER (WHERE t.status = 'pending'), 0) AS pending_count,
    coalesce(count(*) FILTER (
      WHERE t.sla_due_at IS NOT NULL
        AND t.resolved_at IS NOT NULL
        AND t.sla_due_at < t.resolved_at
    ), 0) AS sla_breach_count
  FROM org_members m
  LEFT JOIN tickets t ON t.assigned_agent_id = m.user_id
  WHERE m.org_id = p_org_id
    AND m.role   = 'agent'
  GROUP BY m.user_id;
$$;
```

- [ ] **Step 7: Apply the fix and re-run — expect pass**

Run:
```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/009_fix_workload_summary_v2_nulls.sql
pytest examples/inbox/tests/test_workload_summary.py -v
```
Expected: PASS.

- [ ] **Step 8: Write the doc page to `website/src/content/docs/examples/inbox/equivalent-query-optimization.md`**

```markdown
---
title: Optimizing a query without changing its behavior
description: An equivalence property catches a subtle NULL-vs-0 divergence between two query shapes that "should be" interchangeable.
---

## Problem

`agent_workload_summary(org_id)` returns one row per agent in the org with their open count, pending count, and SLA breach count. It uses one correlated subquery per metric — readable, but slow as `tickets` grows. A senior engineer rewrites it as a single LEFT JOIN with `FILTER` aggregations. The query plan is much better. The dashboard now reports `null` instead of `0` for new agents with no assigned tickets — but the bug doesn't surface for a week, because nobody hires agents during that window.

## v1: the slow version

```sql
SELECT
  m.user_id,
  (SELECT count(*) FROM tickets t WHERE t.assigned_agent_id = m.user_id AND t.status = 'open')    AS open_count,
  ...
FROM org_members m WHERE m.org_id = p_org_id AND m.role = 'agent';
```

## v2: the optimization candidate

```sql
SELECT
  m.user_id,
  count(*) FILTER (WHERE t.status = 'open')    AS open_count,
  ...
FROM org_members m
LEFT JOIN tickets t ON t.assigned_agent_id = m.user_id
WHERE m.org_id = p_org_id AND m.role = 'agent'
GROUP BY m.user_id;
```

## The example test (passing)

```python
def test_v2_returns_counts(db, agent_with_tickets):
    rows = db.query("SELECT * FROM agent_workload_summary_v2(%s)", agent_with_tickets["org_id"])
    assert rows[0]["open_count"] >= 0
```

Doesn't compare v1 to v2 at all. Doesn't try an agent with zero tickets.

## The SqlProof property

```python
@given(data=st.data())
def test_workload_summary_v1_equivalent_to_v2(proof, data):
    dataset = data.draw(proof.dataset_strategy(
        sizes={"org_members": 3, "tickets": st.integers(min_value=0, max_value=10)},
        columns={"org_members.role": st.just("agent")},
    ))
    with proof.client_for_dataset(dataset) as db:
        v1 = sorted(db.query("SELECT * FROM agent_workload_summary_v1(%s)", org_id), key=...)
        v2 = sorted(db.query("SELECT * FROM agent_workload_summary_v2(%s)", org_id), key=...)
        assert v1 == v2
```

## The counterexample

```
Property failed: v1 != v2
  v1=[(agent1, 0, 0, 0)]
  v2=[(agent1, NULL, NULL, NULL)]
```

## The fix

`coalesce(count(*) FILTER (...), 0)` on every aggregate in v2.

## Lifecycle: when to write and when to delete

This is the recipe's most distinctive teaching beat. Equivalence properties are **scaffolding**, not forever-tests.

1. **Local, during the refactor PR.** Engineer writes v2 and this property in the same commit, iterates until Hypothesis can't find a divergence.
2. **CI on the PR (required check).** Hypothesis runs more examples than the engineer ran locally; the persisted counterexample database under `.sqlproof/failures/` travels with the PR.
3. **CI on `main` during the deprecation window.** v1 and v2 both ship; callers migrate from v1 to v2 over ~one week; this property runs on every commit during the window, catching anyone who tweaks v2 in an unrelated PR and silently breaks equivalence.
4. **Deleted with v1.** Once callers are off v1 and v1 is dropped, the property goes too. Keeping v1 alive just to host the property creates perpetual dead code.

**The exception**: deprecation views, dual-writes, compatibility shims across a versioned API boundary. There the property really is forever.

The other recipes in this section guard *permanent* invariants. This one guards a *transient* one — and that's the point.
```

- [ ] **Step 9: Commit**

```bash
git add examples/inbox/schema/001_initial.sql examples/inbox/schema/008_add_workload_summary_v2.sql examples/inbox/schema/009_fix_workload_summary_v2_nulls.sql examples/inbox/tests/test_workload_summary.py website/src/content/docs/examples/inbox/equivalent-query-optimization.md
git commit -m "feat(examples): inbox recipe 7 — equivalent query optimization"
```

---

### Task 10: Recipe 1 — tenant-scoped-vector-search

**Files:**
- Modify: `examples/inbox/schema/001_initial.sql` (append buggy `find_similar_tickets` RPC)
- Create: `examples/inbox/schema/002_fix_similar_tickets.sql`
- Create: `examples/inbox/tests/test_similar_tickets.py`
- Create: `website/src/content/docs/examples/inbox/tenant-scoped-vector-search.md`

The RPC searches similar tickets by message embedding but never filters by `org_id`, leaking nearest neighbors across all tenants.

- [ ] **Step 1: Append the buggy RPC to `001_initial.sql`**

Append at the end of the file:

```sql
-- ---------------------------------------------------------------------------
-- Recipe 1 (tenant-scoped-vector-search) — BUGGY similar-ticket RPC
-- ---------------------------------------------------------------------------

-- BUG: returns the k nearest tickets by embedding distance, but never
-- filters by org_id. A ticket in org A finds matches from org B.
-- Reviewers see a sensible-looking similarity query.
CREATE OR REPLACE FUNCTION find_similar_tickets(
  p_ticket_id UUID,
  p_k INT DEFAULT 5
)
  RETURNS TABLE (ticket_id UUID, distance double precision)
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  WITH target AS (
    SELECT me.embedding
    FROM message_embeddings me
    JOIN messages m ON m.id = me.message_id
    WHERE m.ticket_id = p_ticket_id
    LIMIT 1
  )
  SELECT m.ticket_id, (me.embedding <-> (SELECT embedding FROM target)) AS distance
  FROM message_embeddings me
  JOIN messages m ON m.id = me.message_id
  WHERE m.ticket_id <> p_ticket_id
  ORDER BY distance ASC
  LIMIT p_k;
$$;

GRANT EXECUTE ON FUNCTION find_similar_tickets(UUID, INT) TO authenticated;
```

- [ ] **Step 2: Write the failing test to `examples/inbox/tests/test_similar_tickets.py`**

```python
"""Recipe 1: tenant-scoped-vector-search.

`find_similar_tickets` returns the k nearest neighbors by message
embedding distance but never filters by org_id. A ticket in org A
finds matches from org B.

Uses `vector_strategy` (see _helpers.py) to work around SqlProof's
pending pgvector parser support (issue #69).
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.contrib.supabase import as_rls_user

from _helpers import vector_strategy

PROOF = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@PROOF
@given(data=st.data())
def test_similar_tickets_are_all_in_the_input_org(supabase_proof, data) -> None:
    dataset = data.draw(
        supabase_proof.dataset_strategy(
            sizes={
                "organizations":      2,
                "customers":          2,
                "org_members":        2,
                "tickets":            4,
                "messages":           4,
                "message_embeddings": 4,
            },
            columns={
                "message_embeddings.embedding": vector_strategy(384),
            },
        ),
    )
    with supabase_proof.client_for_dataset(dataset) as db:
        input_ticket = dataset["tickets"][0]
        member_of_input_org = next(
            m for m in dataset["org_members"]
            if m["org_id"] == input_ticket["org_id"]
        )

        with as_rls_user(db, member_of_input_org["user_id"]):
            rows = db.query(
                "SELECT ticket_id FROM find_similar_tickets(%s::uuid, 5)",
                input_ticket["id"],
            )

        if not rows:
            return  # let Hypothesis re-draw if no other ticket had an embedding

        returned_ticket_ids = [r["ticket_id"] for r in rows]
        returned_org_ids = db.query(
            "SELECT id, org_id FROM tickets WHERE id = ANY(%s::uuid[])",
            returned_ticket_ids,
        )
        cross_tenant = [
            t for t in returned_org_ids
            if t["org_id"] != input_ticket["org_id"]
        ]
        assert cross_tenant == [], (
            f"vector search leaked across tenants: {cross_tenant}"
        )
```

- [ ] **Step 3: Run the test — expect failure**

Run:
```bash
pytest examples/inbox/tests/test_similar_tickets.py -v
```
Expected: FAIL with a counterexample showing a returned `ticket_id` from a different org.

- [ ] **Step 4: Write the fix migration to `examples/inbox/schema/002_fix_similar_tickets.sql`**

```sql
-- Recipe 1 fix: scope the search to the input ticket's org_id.

CREATE OR REPLACE FUNCTION find_similar_tickets(
  p_ticket_id UUID,
  p_k INT DEFAULT 5
)
  RETURNS TABLE (ticket_id UUID, distance double precision)
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  WITH input AS (
    SELECT t.org_id, me.embedding
    FROM tickets t
    JOIN messages m            ON m.ticket_id = t.id
    JOIN message_embeddings me ON me.message_id = m.id
    WHERE t.id = p_ticket_id
    LIMIT 1
  )
  SELECT m.ticket_id,
         (me.embedding <-> (SELECT embedding FROM input)) AS distance
  FROM message_embeddings me
  JOIN messages m ON m.id = me.message_id
  JOIN tickets  t ON t.id = m.ticket_id
  WHERE t.org_id    = (SELECT org_id FROM input)
    AND m.ticket_id <> p_ticket_id
  ORDER BY distance ASC
  LIMIT p_k;
$$;
```

- [ ] **Step 5: Apply the fix and re-run the test**

Run:
```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/002_fix_similar_tickets.sql
pytest examples/inbox/tests/test_similar_tickets.py -v
```
Expected: PASS.

- [ ] **Step 6: Write the doc page to `website/src/content/docs/examples/inbox/tenant-scoped-vector-search.md`**

```markdown
---
title: Vector search that leaks across tenants
description: A SECURITY DEFINER similarity-search RPC forgets to filter by org_id, returning nearest neighbors from every tenant.
---

## Problem

You ship `find_similar_tickets(ticket_id)` so agents triaging a new ticket see similar past tickets. It runs as `SECURITY DEFINER` because it touches `message_embeddings`. Months later, an agent in org A pulls up a ticket and the "similar tickets" panel shows a customer-support ticket from org B that happens to embed close in vector space.

## The code

```sql
CREATE FUNCTION find_similar_tickets(p_ticket_id UUID, p_k INT DEFAULT 5)
RETURNS TABLE (...) SECURITY DEFINER AS $$
  SELECT m.ticket_id, (me.embedding <-> ...) AS distance
  FROM message_embeddings me
  JOIN messages m ON m.id = me.message_id
  WHERE m.ticket_id <> p_ticket_id
  ORDER BY distance ASC LIMIT p_k;
$$;
```

## Why review misses it

Two failure modes compound here. First, `SECURITY DEFINER` bypasses caller RLS — so RLS on `tickets` doesn't save you. Second, the function reads as a "find the nearest neighbors" query, and reviewers don't typically read those queries asking "do they cross a security boundary?" The org filter belongs in the function body, not in the policy layer.

## The example test that passes

```python
def test_returns_some_neighbors(db, org_with_tickets):
    rows = db.query("SELECT * FROM find_similar_tickets(%s)", org_with_tickets["tickets"][0]["id"])
    assert len(rows) > 0
```

Seeds one org. No cross-tenant leak possible.

## The SqlProof property

```python
@given(data=st.data())
def test_similar_tickets_are_all_in_the_input_org(supabase_proof, data):
    dataset = data.draw(supabase_proof.dataset_strategy(
        sizes={"organizations": 2, "tickets": 4, "message_embeddings": 4},
        columns={"message_embeddings.embedding": vector_strategy(384)},
    ))
    ...
    assert [t for t in returned if t["org_id"] != input_ticket["org_id"]] == []
```

Two orgs is the minimum that makes the bug visible. Hypothesis generates them.

## The counterexample

```
Property failed: vector search leaked across tenants
Returned ticket from org B while input ticket was in org A
```

## The fix

Resolve the input ticket's `org_id` in a CTE and filter the search to it:

```sql
WITH input AS (
  SELECT t.org_id, me.embedding FROM tickets t ...
  WHERE t.id = p_ticket_id LIMIT 1
)
... WHERE t.org_id = (SELECT org_id FROM input)
```

## Related

For the general "SECURITY DEFINER bypasses RLS" pattern, see the [RLS bug-classes reference](/guides/supabase-rls-bug-classes/).
```

- [ ] **Step 7: Commit**

```bash
git add examples/inbox/schema/001_initial.sql examples/inbox/schema/002_fix_similar_tickets.sql examples/inbox/tests/test_similar_tickets.py website/src/content/docs/examples/inbox/tenant-scoped-vector-search.md
git commit -m "feat(examples): inbox recipe 1 — tenant-scoped vector search"
```

---

### Task 11: Recipe 6 — stable-vector-pagination

**Files:**
- Modify: `examples/inbox/schema/001_initial.sql` (append buggy `search_kb_hybrid` RPC)
- Create: `examples/inbox/schema/007_fix_hybrid_search.sql`
- Create: `examples/inbox/tests/test_hybrid_search.py`
- Create: `website/src/content/docs/examples/inbox/stable-vector-pagination.md`

Hybrid search combines text and vector scores and paginates with `LIMIT/OFFSET` but has no tiebreaker — tied scores cause duplicates or gaps across pages.

- [ ] **Step 1: Append the buggy RPC to `001_initial.sql`**

Append at the end of the file:

```sql
-- ---------------------------------------------------------------------------
-- Recipe 6 (stable-vector-pagination) — BUGGY hybrid search RPC
-- ---------------------------------------------------------------------------

-- BUG: ORDER BY combined_score with no tiebreaker. When multiple
-- articles tie on score (common with short queries or sparse
-- embeddings), Postgres's tie-breaking is implementation-defined;
-- the same article can appear on two pages or vanish entirely.
CREATE OR REPLACE FUNCTION search_kb_hybrid(
  p_org_id           UUID,
  p_query_embedding  vector(384),
  p_text_query       TEXT,
  p_limit            INT DEFAULT 5,
  p_offset           INT DEFAULT 0
)
  RETURNS TABLE (article_id UUID, score double precision)
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  SELECT
    a.id AS article_id,
    (
      0.7 * (1 - (ae.embedding <=> p_query_embedding))
      + 0.3 * CASE WHEN a.title ILIKE '%' || p_text_query || '%' THEN 1.0 ELSE 0.0 END
    ) AS score
  FROM kb_articles a
  JOIN kb_article_embeddings ae ON ae.article_id = a.id
  WHERE a.org_id = p_org_id
  ORDER BY score DESC
  LIMIT p_limit OFFSET p_offset;
$$;

GRANT EXECUTE ON FUNCTION search_kb_hybrid(UUID, vector, TEXT, INT, INT) TO authenticated;
```

- [ ] **Step 2: Write the failing pagination test to `examples/inbox/tests/test_hybrid_search.py`**

```python
"""Recipe 6: stable-vector-pagination.

The pagination property: paging through all results in chunks of N
yields the same set as fetching all results in one query, with no
duplicates and no skipped articles. The buggy ORDER BY has no
tiebreaker, so identical scores break pagination.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from _helpers import vector_strategy

PROOF = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@PROOF
@given(
    data=st.data(),
    query_text=st.sampled_from(["a", "the", "support"]),
    page_size=st.integers(min_value=1, max_value=3),
)
def test_pagination_partitions_full_result_set(
    proof, data, query_text, page_size,
) -> None:
    # Force many tied scores by making all embeddings identical and
    # all titles miss the query text — every article gets score 0.7.
    fixed_vec = "[" + ",".join(["0.0"] * 384) + "]"

    dataset = data.draw(
        proof.dataset_strategy(
            sizes={
                "organizations":          1,
                "kb_articles":            8,
                "kb_article_embeddings":  8,
            },
            columns={
                "kb_article_embeddings.embedding": st.just(fixed_vec),
                "kb_articles.title": st.just("zzz_nomatch_zzz"),
                "kb_articles.published": st.just(True),
            },
        ),
    )
    with proof.client_for_dataset(dataset) as db:
        org_id = dataset["organizations"][0]["id"]

        all_at_once = db.query(
            "SELECT article_id FROM search_kb_hybrid(%s, %s::vector, %s, 100, 0)",
            org_id, fixed_vec, query_text,
        )
        full_set = {row["article_id"] for row in all_at_once}

        paged_ids: list[str] = []
        offset = 0
        while True:
            page = db.query(
                "SELECT article_id FROM search_kb_hybrid(%s, %s::vector, %s, %s, %s)",
                org_id, fixed_vec, query_text, page_size, offset,
            )
            if not page:
                break
            paged_ids.extend(row["article_id"] for row in page)
            offset += page_size

        assert len(paged_ids) == len(set(paged_ids)), (
            f"duplicate article ids across pages: "
            f"{[x for x in paged_ids if paged_ids.count(x) > 1]}"
        )
        assert set(paged_ids) == full_set, (
            f"paged set != full set: "
            f"missing={full_set - set(paged_ids)}, "
            f"extra={set(paged_ids) - full_set}"
        )
```

- [ ] **Step 3: Run the test — expect failure**

Run:
```bash
pytest examples/inbox/tests/test_hybrid_search.py -v
```
Expected: FAIL — duplicates or missing articles across pages.

- [ ] **Step 4: Write the fix migration to `examples/inbox/schema/007_fix_hybrid_search.sql`**

```sql
-- Recipe 6 fix: add `article_id` as a tiebreaker so the ORDER BY
-- is total. Pagination is now stable across queries and against
-- concurrent inserts.

CREATE OR REPLACE FUNCTION search_kb_hybrid(
  p_org_id           UUID,
  p_query_embedding  vector(384),
  p_text_query       TEXT,
  p_limit            INT DEFAULT 5,
  p_offset           INT DEFAULT 0
)
  RETURNS TABLE (article_id UUID, score double precision)
  LANGUAGE sql STABLE
  SECURITY DEFINER
  SET search_path = public
AS $$
  SELECT
    a.id AS article_id,
    (
      0.7 * (1 - (ae.embedding <=> p_query_embedding))
      + 0.3 * CASE WHEN a.title ILIKE '%' || p_text_query || '%' THEN 1.0 ELSE 0.0 END
    ) AS score
  FROM kb_articles a
  JOIN kb_article_embeddings ae ON ae.article_id = a.id
  WHERE a.org_id = p_org_id
  ORDER BY score DESC, a.id ASC   -- the missing tiebreaker
  LIMIT p_limit OFFSET p_offset;
$$;
```

- [ ] **Step 5: Apply the fix and re-run the test**

Run:
```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/007_fix_hybrid_search.sql
pytest examples/inbox/tests/test_hybrid_search.py -v
```
Expected: PASS.

- [ ] **Step 6: Write the doc page to `website/src/content/docs/examples/inbox/stable-vector-pagination.md`**

```markdown
---
title: Pagination breaks on tied similarity scores
description: An ORDER BY without a tiebreaker over a similarity score quietly drops and duplicates results across pages.
---

## Problem

You ship a hybrid-search API combining vector and text scores. Paginate with `LIMIT 5 OFFSET 0`, then `LIMIT 5 OFFSET 5`, etc. Users on the second page see articles they already saw on the first, and articles they should see vanish entirely.

## The code

```sql
SELECT a.id, (vector_part + text_part) AS score
FROM kb_articles a JOIN kb_article_embeddings ae ...
ORDER BY score DESC
LIMIT p_limit OFFSET p_offset;
```

## Why review misses it

The reviewer sees `ORDER BY score DESC` and assumes stable ordering. Postgres doesn't promise a stable order for tied rows — and ties are far more common in hybrid search (sparse embeddings, short text matches that snap to 0/1) than reviewers expect.

## The example test that passes

```python
def test_first_page_has_results(db, org_with_articles):
    rows = db.query("SELECT * FROM search_kb_hybrid(%s, ...) LIMIT 5 OFFSET 0", org_id)
    assert len(rows) > 0
```

Doesn't paginate. Doesn't compare across pages.

## The SqlProof property

```python
@given(data=st.data(), page_size=st.integers(min_value=1, max_value=3))
def test_pagination_partitions_full_result_set(proof, data, page_size):
    # Force ties: identical embeddings, identical titles.
    dataset = data.draw(proof.dataset_strategy(
        sizes={"kb_articles": 8, "kb_article_embeddings": 8},
        columns={"kb_article_embeddings.embedding": st.just(fixed_vec), ...},
    ))
    full_set  = set(db.query("... LIMIT 100"))
    paged_ids = paginate_through_all(db, page_size)
    assert len(paged_ids) == len(set(paged_ids))    # no duplicates
    assert set(paged_ids) == full_set               # nothing missing
```

The teaching beat: when you can articulate the property as "paging is a partition of the full set," any pagination bug is testable.

## The counterexample

```
Property failed: duplicate article ids across pages
page_size=2, articles=[a1, a2, a3, a4]
page 0 = [a3, a1], page 1 = [a3, a2], page 2 = [a4]
```

## The fix

Add a stable tiebreaker on a unique column:

```sql
ORDER BY score DESC, a.id ASC
```
```

- [ ] **Step 7: Commit**

```bash
git add examples/inbox/schema/001_initial.sql examples/inbox/schema/007_fix_hybrid_search.sql examples/inbox/tests/test_hybrid_search.py website/src/content/docs/examples/inbox/stable-vector-pagination.md
git commit -m "feat(examples): inbox recipe 6 — stable vector pagination"
```

---

### Task 12: Index page + reference page

**Files:**
- Create: `website/src/content/docs/examples/inbox/index.md`
- Create: `website/src/content/docs/guides/supabase-rls-bug-classes.md`

- [ ] **Step 1: Write the inbox index page**

```markdown
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
| [Pagination breaks on tied scores](stable-vector-pagination) | Round-trip (paginated set equality) | `ORDER BY score` has no tiebreaker |
| [Equivalent query optimization](equivalent-query-optimization) | Equivalence / migration safety | v2's `LEFT JOIN + FILTER` returns `NULL` where v1 returns `0` |
| [Stateful ticket lifecycle](stateful-ticket-lifecycle) | Stateful (sequence-dependent) | `reopen_ticket` doesn't clear `resolved_at` |
| [Mass assignment without WITH CHECK](mass-assignment-without-with-check) | RLS regression (write side) | UPDATE policy lets members change any column of their own row |
| [Overly permissive DELETE policy](missing-delete-policy) | RLS regression (write side) | DELETE policy with `USING (true)` lets viewers eject admins |

For smaller RLS bug classes that don't justify a full case study (over-permissive `USING (true)`, UPDATE-without-SELECT silent fail, `security_invoker` view bypass, `user_metadata` trust, infinite policy recursion, plus schema-level audits like "RLS enabled on every public table"), see the [Supabase RLS bug classes](/guides/supabase-rls-bug-classes/) reference page.

## Caveats

- The buggy code in this sample is intentional. **Do not deploy this schema as-is**.
- Embeddings in tests are random — these recipes test schema-level invariants, not retrieval quality. Plugging in a real embedding model is a separate concern.
- Recipes 1 and 6 depend on a pgvector parser workaround (`vector_strategy(384)` in `tests/_helpers.py`) until [SqlProof issue #69](https://github.com/alialavia/sqlproof/issues/69) lands.
```

- [ ] **Step 2: Write the RLS reference page**

```markdown
---
title: Supabase RLS bug classes — reference
description: A compact catalog of common Supabase RLS bug shapes and the SqlProof properties that catch each one.
---

This page catalogs the RLS bug classes that don't get full case studies in the [Inbox sample](/examples/inbox/). One paragraph per bug, one short SqlProof property snippet, plus an audit-style snippet for the schema-level ones.

For the fuller, recipe-style treatment of the most-impactful RLS bugs (tenant scoping in `SECURITY DEFINER` RPCs, uncorrelated `EXISTS`, missing `WITH CHECK`, missing DELETE policies, internal-vs-public message gating), see the [Inbox sample recipes](/examples/inbox/).

## Over-permissive policy (`USING (true)`)

A policy `USING (true)` on a SELECT/INSERT/UPDATE/DELETE grants the operation to any caller in the policy's role. Sometimes intentional for public-read tables, often a copy-paste error from a "make it work first" prototype.

```python
@given(data=st.data())
def test_policy_excludes_some_rows(supabase_proof, data):
    # If the policy is restrictive at all, at least one non-owner should
    # see fewer rows than the owner. If they see all rows, the policy is
    # equivalent to USING (true).
    ...
```

## UPDATE policy without a paired SELECT policy

PostgreSQL evaluates UPDATE policies by first reading the row (via the SELECT policy chain). With no SELECT policy on the table, the read returns nothing, and the UPDATE silently affects zero rows even for the owner.

```python
def test_owner_update_actually_modifies_the_row(supabase_proof, data):
    with as_rls_user(db, owner_id):
        db.execute("UPDATE table SET col = %s WHERE id = %s", new_val, row_id)
    after = db.scalar("SELECT col FROM table WHERE id = %s", row_id)
    assert after == new_val   # fails silently if SELECT policy missing
```

## `security_invoker` view bypass

Pre-PG15, views run as their creator (often `postgres`), so the underlying table's RLS is ignored. In PG15+ create views with `WITH (security_invoker = true)`.

```python
def test_view_respects_underlying_rls(supabase_proof, data):
    with as_rls_user(db, non_owner_id):
        via_view  = db.query("SELECT id FROM the_view WHERE id = %s", row_id)
        direct    = db.query("SELECT id FROM the_table WHERE id = %s", row_id)
    assert via_view == direct
```

## `user_metadata` trust

Supabase exposes `auth.jwt() -> 'user_metadata'` as JWT claims that the *client* can write. Policies that trust `user_metadata->>'role' = 'admin'` are bypassable; use `app_metadata` (server-only) or a database table.

SqlProof's `as_supabase_user` accepts an `extra_claims=` arg, so you can directly test that a user *with* an `admin` claim in `user_metadata` doesn't gain admin powers:

```python
with as_rls_user(db, user_id, extra_claims={"user_metadata": {"role": "admin"}}):
    rows = db.query("SELECT * FROM admin_only_table")
assert rows == []   # if the policy correctly ignores user_metadata
```

## Infinite policy recursion

A policy on table `A` that references table `B`, whose policy references table `A`, can cause PostgreSQL to recurse until `stack depth limit exceeded`. The property: every policy-gated query completes within a reasonable budget.

```python
def test_policy_query_does_not_recurse(supabase_proof, data):
    with as_rls_user(db, user_id):
        db.query("SELECT id FROM table_a LIMIT 1")
    # If we reach here, no stack overflow.
```

## Schema-level audits (one-shot, not property tests)

These are introspection queries, not invariants — run them once per CI build to enforce shop-wide RLS hygiene.

### Every public table has RLS enabled

```python
def test_every_public_table_has_rls(db):
    rows = db.query("""
        SELECT c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind = 'r' AND n.nspname = 'public' AND NOT c.relrowsecurity
    """)
    assert rows == [], f"tables without RLS: {[r['relname'] for r in rows]}"
```

### Every policy specifies a `TO` clause

```python
def test_every_policy_targets_a_role(db):
    rows = db.query("""
        SELECT schemaname, tablename, policyname
        FROM pg_policies
        WHERE roles = '{public}'   -- public means "all roles", almost always a mistake
    """)
    assert rows == []
```

### Every `SECURITY DEFINER` function pins `search_path`

```python
def test_security_definer_functions_lock_search_path(db):
    rows = db.query("""
        SELECT n.nspname, p.proname
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE p.prosecdef = true
          AND n.nspname = 'public'
          AND (p.proconfig IS NULL
               OR NOT EXISTS (SELECT 1 FROM unnest(p.proconfig) c WHERE c LIKE 'search_path=%'))
    """)
    assert rows == []
```

When SqlProof ships [issue #77](https://github.com/alialavia/sqlproof/issues/77) (planned `sqlproof.contrib.supabase.audit` module), these snippets will become one-liners like `assert_rls_enabled(db, "tickets")` and `tables_without_rls(db) == set()`.

## Identity / silent-fail bugs

When `auth.uid()` returns NULL (anonymous request) and a policy like `USING (auth.uid() = user_id)` evaluates to NULL → false, the API returns an empty result. Test the anonymous path explicitly — see [SqlProof issue #78](https://github.com/alialavia/sqlproof/issues/78) for the planned `as_anonymous()` helper; today's workaround:

```python
db.execute("SET LOCAL ROLE anon")
try:
    rows = db.query("SELECT * FROM tickets")
finally:
    db.execute("RESET ROLE")
assert rows == []
```
```

- [ ] **Step 3: Commit**

```bash
git add website/src/content/docs/examples/inbox/index.md website/src/content/docs/guides/supabase-rls-bug-classes.md
git commit -m "docs(examples): inbox index + RLS bug-classes reference page"
```

---

### Task 13: Docs site integration — sidebar + cross-refs

**Files:**
- Modify: `website/astro.config.mjs` (add sidebar entries)
- Modify: `website/src/content/docs/examples/property-patterns.md` (cross-ref inbox recipes)

- [ ] **Step 1: Add sidebar entries to `website/astro.config.mjs`**

Modify the `sidebar` array. In the `Test Patterns` group, append the Inbox sample subsection; in the `Supabase` group, append the bug-classes reference. The result should look like:

```javascript
{
  label: 'Test Patterns',
  items: [
    { label: 'Five Property Patterns', slug: 'examples/property-patterns' },
    { label: 'Testing SQL Functions', slug: 'examples/testing-sql-functions' },
    { label: 'Stateful Tests', slug: 'api/state-machine' },
    { label: 'Realistic Data Generation', slug: 'examples/data-generation' },
    { label: 'E-Commerce Orders Walkthrough', slug: 'examples/orders' },
    {
      label: 'Inbox sample (full Supabase app)',
      items: [
        { label: 'Overview', slug: 'examples/inbox/index' },
        { label: '1. Tenant-scoped vector search', slug: 'examples/inbox/tenant-scoped-vector-search' },
        { label: '2. Correlated RLS subqueries', slug: 'examples/inbox/correlated-rls-subqueries' },
        { label: '3. Idempotent status triggers', slug: 'examples/inbox/idempotent-status-triggers' },
        { label: '4. Outer joins and WHERE', slug: 'examples/inbox/outer-joins-and-where' },
        { label: '5. Internal-message RLS', slug: 'examples/inbox/internal-message-rls' },
        { label: '6. Stable vector pagination', slug: 'examples/inbox/stable-vector-pagination' },
        { label: '7. Equivalent query optimization', slug: 'examples/inbox/equivalent-query-optimization' },
        { label: '8. Stateful ticket lifecycle', slug: 'examples/inbox/stateful-ticket-lifecycle' },
        { label: '9. Mass assignment without WITH CHECK', slug: 'examples/inbox/mass-assignment-without-with-check' },
        { label: '10. Missing DELETE policy', slug: 'examples/inbox/missing-delete-policy' },
      ],
    },
  ],
},
{
  label: 'Supabase',
  items: [
    { label: 'Testing Supabase Apps', slug: 'guides/supabase' },
    { label: 'RLS bug classes (reference)', slug: 'guides/supabase-rls-bug-classes' },
  ],
},
```

- [ ] **Step 2: Add cross-ref paragraph to `property-patterns.md`**

Append at the end of `website/src/content/docs/examples/property-patterns.md`:

```markdown
## Worked examples in the Inbox sample

Every pattern above has a fully-worked, runnable recipe in the [Inbox sample](/examples/inbox/) — a multi-tenant Supabase support inbox where each recipe pairs a buggy production-shape RPC/policy/trigger with the SqlProof property that catches it:

- **Aggregation invariant** → [Outer joins and WHERE](/examples/inbox/outer-joins-and-where/)
- **RLS policy regression** → [Correlated RLS subqueries](/examples/inbox/correlated-rls-subqueries/), [Internal-message RLS](/examples/inbox/internal-message-rls/), [Mass assignment](/examples/inbox/mass-assignment-without-with-check/), [Missing DELETE policy](/examples/inbox/missing-delete-policy/), [Tenant-scoped vector search](/examples/inbox/tenant-scoped-vector-search/)
- **Migration safety / equivalence** → [Equivalent query optimization](/examples/inbox/equivalent-query-optimization/)
- **Idempotency** → [Idempotent status triggers](/examples/inbox/idempotent-status-triggers/)
- **Round-trip serialization** → [Stable vector pagination](/examples/inbox/stable-vector-pagination/) (paginated-set equality is the same shape)

Plus one **stateful (sequence-dependent)** pattern not represented above: [Stateful ticket lifecycle](/examples/inbox/stateful-ticket-lifecycle/).
```

- [ ] **Step 3: Build the docs site locally and verify**

Run:
```bash
cd website
npm run build
```
Expected: build succeeds with no broken-link warnings. The new sidebar entries appear in the rendered output (you can open the built `dist/` directory or run `npm run dev` and visit each new page).

- [ ] **Step 4: Commit**

```bash
git add website/astro.config.mjs website/src/content/docs/examples/property-patterns.md
git commit -m "docs(site): add Inbox sample + RLS reference to sidebar and cross-refs"
```

---

## End-to-end verification

Once all tasks land, the final repro flow should work cleanly:

- [ ] **Step 1: Drop and recreate the public schema**

Run:
```bash
psql "$SUPABASE_DB_URL" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public; GRANT ALL ON SCHEMA public TO postgres, public;"
```

- [ ] **Step 2: Apply the initial schema**

Run:
```bash
psql "$SUPABASE_DB_URL" -f examples/inbox/schema/001_initial.sql
```

- [ ] **Step 3: Run all tests — expect 9 failures + 1 skipped**

Run:
```bash
pytest examples/inbox/tests -v
```
Expected counts:
- `test_smoke.py`: 2 passed
- `test_tickets_rls.py`: 1 failed
- `test_messages_rls.py`: 1 failed
- `test_resolved_at_trigger.py`: 1 failed
- `test_dashboard.py`: 1 failed, 1 passed (the missing-status-bucket assertion fails; the sum-of-counts assertion holds even with the bug because dropping zero-count buckets doesn't change the sum)
- `test_org_members_mass_assignment.py`: 1 failed
- `test_org_members_delete_policy.py`: 1 failed
- `test_ticket_lifecycle.py`: 1 failed
- `test_workload_summary.py`: 1 skipped
- `test_similar_tickets.py`: 1 failed
- `test_hybrid_search.py`: 1 failed

Total: 3 passed, 9 failed, 1 skipped. (One failure per recipe.)

- [ ] **Step 4: Apply all fix migrations + load v2**

Run:
```bash
for f in 002 003 004 005 006 007 008 009 010 011 012; do
  psql "$SUPABASE_DB_URL" -f examples/inbox/schema/${f}_*.sql
done
```

- [ ] **Step 5: Re-run all tests — expect all passing**

Run:
```bash
pytest examples/inbox/tests -v
```
Expected: all green.

---

## Spec coverage review

After implementing all tasks above, the spec's deliverables map to tasks as follows:

| Spec item | Task |
|-----------|------|
| Ten tables (schema) | Task 1 |
| Recipe 1 (tenant-scoped-vector-search) | Task 10 |
| Recipe 2 (correlated-rls-subqueries) | Task 2 |
| Recipe 3 (idempotent-status-triggers) | Task 4 |
| Recipe 4 (outer-joins-and-where) | Task 5 |
| Recipe 5 (internal-message-rls) | Task 3 |
| Recipe 6 (stable-vector-pagination) | Task 11 |
| Recipe 7 (equivalent-query-optimization) | Task 9 |
| Recipe 8 (stateful-ticket-lifecycle) | Task 8 |
| Recipe 9 (mass-assignment-without-with-check) | Task 6 |
| Recipe 10 (missing-delete-policy) | Task 7 |
| Inbox index page | Task 12 |
| RLS bug-classes reference page | Task 12 |
| Sidebar entries + cross-refs | Task 13 |
| README with repro flow | Task 1 |
| `_helpers.py` with `vector_strategy(384)` | Task 1 |
| pgvector parser workaround documented | Plan header + Task 1 |
| Recipe 7 skip pattern when v2 not loaded | Task 9 step 3 |

All spec items have implementing tasks. The "What's deferred to v2" section of the spec (Supabase Storage, embedding-model integration, CI smoke job) intentionally has no tasks here — it's out of scope.

---

## Notes for executor

- **Don't deploy any partial state of the schema.** Each fix migration assumes the prior buggy version is in place; out-of-order application produces undefined behavior. The `DROP SCHEMA public CASCADE` step at the start of end-to-end verification resets everything.
- **The pgvector workaround is the only place where SqlProof's pending issue (#69) is felt.** If #69 lands during implementation, simplify recipes 1 and 6 by removing the `columns={"...embedding": vector_strategy(384)}` override — the test should then "just work" with the generator's native vector support.
- **Recipe 5's customer-side test depends on the `customer_id` JWT claim shape used by the policy.** If the JWT shape changes during implementation, update both the policy's `auth.jwt() ->> 'customer_id'` and the test's `extra_claims={"customer_id": ...}` in lockstep.
- **Each recipe is independent.** If a task hits an unexpected blocker, the implementer can skip to the next recipe — the foundation task (1) is the only sequential prerequisite.
