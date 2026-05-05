---
title: Test your Supabase project in 60 seconds
description: Drop SqlProof into a Supabase project and let your AI coding agent write the tests. Setup, agent rules file, and the three test patterns you need.
---

You're a solo founder building on Supabase. Your schema lives in
`supabase/schemas/`, your RLS policies are doing real work, and you'd
sleep better at night if you knew they were correct. You don't write
tests by hand — you ask Claude / Cursor / your agent of choice to do
that. SqlProof is built for this.

This page is the 60-second path: install, drop in two files, ask your
agent to write tests.

## 1. Install (one command)

Make sure your project has Python 3.11+ and `pytest`. Then:

```bash
pip install --pre sqlproof
```

The `--pre` flag is required because SqlProof is currently in alpha
(0.1.0a1). Until 1.0, every install needs `--pre` (or `--prerelease=allow`
if you're using `uv`).

## 2. Drop in `tests/conftest.py`

This connects SqlProof to your local Supabase. The user / password / port
match `supabase start` defaults — adjust if you've changed them.

```python
"""SqlProof test setup for a Supabase project."""

from __future__ import annotations

import os
import pytest

from sqlproof import SqlProof
from sqlproof.contrib.supabase import seed_test_users_directly

DATABASE_URL = os.environ.get(
    "SUPABASE_DB_URL",
    "postgresql://postgres:postgres@127.0.0.1:54322/postgres",
)


@pytest.fixture(scope="session")
def proof():
    proof = SqlProof.from_connection_string(DATABASE_URL)
    try:
        from psycopg import connect
        from psycopg.rows import dict_row
        from sqlproof.client import PsycopgSqlProofClient

        with connect(DATABASE_URL, autocommit=True, row_factory=dict_row) as conn:
            seed_test_users_directly(PsycopgSqlProofClient(conn), count=5)

        yield proof
    finally:
        proof.disconnect()


@pytest.fixture
def db(proof):
    """Per-test database client. All inserts roll back on test exit."""
    with proof.client_for_dataset({}) as client:
        yield client
```

## 3. Drop in `AGENTS.md` at the project root

Copy [the SqlProof AGENTS.md from the repo](https://github.com/alialavia/sqlproof/blob/main/AGENTS.md)
into your project root.

This file primes your AI agent (Claude Code, Cursor, Aider, anything
else that reads `AGENTS.md` / `.cursorrules` / `CLAUDE.md`) on the
exact patterns SqlProof expects. It contains:

- The three test patterns you'll actually use (RLS policy, RPC
  function, stateful sequence).
- Anti-patterns the agent commonly gets wrong (manually setting JWT
  claims, forgetting `::cast` in raw SQL, etc.).
- File and naming conventions so test names read like sentences.

When you ask the agent "write a test that the project owner can read
their own projects but not other users' projects," the agent reads
`AGENTS.md` first and produces a test that follows the patterns
exactly.

## 4. Make sure Supabase is running, then ask your agent for a test

```bash
supabase start
```

Then in your editor / agent of choice:

> Write a SqlProof test verifying that the `get_dashboard_summary`
> RPC returns the correct event count for the project owner, and zero
> events for non-members.

A good agent (with `AGENTS.md` loaded) writes something like:

```python
"""Tests for the get_dashboard_summary RPC."""

from uuid import uuid4
from sqlproof.contrib.supabase import as_supabase_user
from sqlproof.client import SqlProofClient


def test_owner_sees_event_count_for_their_project(db: SqlProofClient) -> None:
    owner_id = _insert_user(db)
    project_id = _insert_project(db, owner_id)
    _insert_events(db, project_id, count=3)

    with as_supabase_user(db, owner_id):
        payload = db.scalar(
            "SELECT get_dashboard_summary(%s::uuid)", project_id
        )

    assert payload["event_count"] == 3


def test_non_member_sees_zero_event_count(db: SqlProofClient) -> None:
    owner_id = _insert_user(db)
    other_id = _insert_user(db)
    project_id = _insert_project(db, owner_id)
    _insert_events(db, project_id, count=3)

    with as_supabase_user(db, other_id):
        payload = db.scalar(
            "SELECT get_dashboard_summary(%s::uuid)", project_id
        )

    assert payload["event_count"] == 0


# helpers omitted — agent writes them too
```

```bash
pytest tests/ -v
```

## 5. Run before every `supabase db push`

The wedge: run your SqlProof tests before deploying. If your agent has
been writing tests as you build features, this becomes one habit:

```bash
pytest tests/ && supabase db push
```

If a test fails, the failing test name reads like a sentence
(`test_non_member_sees_zero_event_count` failed). You read it, you
understand what's broken, you fix the migration before it ships.

---

## What kinds of bugs this catches in practice

The bugs SqlProof catches in Supabase projects, in roughly the order
you'll hit them:

### RLS policies that leak data under specific conditions

You write a policy that *looks* correct. It works for the user you
tested with. Then a customer who's a member of two projects and a
viewer on a third sees rows they shouldn't. SqlProof's stateful tests
explore role/membership combinations exhaustively and shrink to the
minimal failing setup.

### RPC functions that return subtly wrong aggregates

A `count(*)` that drifts by one when there are NULL values. A `SUM`
that loses precision on `numeric` after a refactor. A `ROW_NUMBER()
OVER (ORDER BY ...)` that's nondeterministic on tied values.
Property-based tests catch these because they generate hundreds of
valid datasets, including the one that hits the edge case.

### Migrations that change query results without anyone noticing

You "improve" a query — switch from `LEFT JOIN ... IS NULL` to `NOT
EXISTS`, add an index that changes the plan, replace a window function
with a self-join. The new query is *almost* equivalent. Run both
queries against the same generated dataset; assert they match.

### Triggers that fire when they shouldn't (or don't fire when they should)

`updated_at` triggers that fire on UPSERTs even when nothing changed.
Cascade triggers that don't propagate. Easy to verify: insert known
data, check the side-effect column.

---

## Going deeper

Once you have the basics working:

- **More test patterns:** [Five Property Patterns](/examples/property-patterns/) — aggregation invariants, idempotency, round-trip serialization.
- **Function testing in depth:** [Testing SQL Functions](/examples/testing-sql-functions/) — a realistic pricing function with stacked discounts and country-specific rounding, tested two ways.
- **The data generator:** [Realistic Data Generation](/examples/data-generation/) — schema-aware multi-table generation that respects FKs, CHECKs, UNIQUEs. Useful for seeding dev DBs too.
- **Supabase-specific helpers:** [Testing Supabase Apps](/guides/supabase/) — `as_supabase_user`, direct-insert auth-user seeding, external table specs.

## When you hit something the docs don't cover

[Open an issue](https://github.com/alialavia/sqlproof/issues). The
project is small enough that real bug reports get fast attention, and
your specific Supabase use case probably isn't unique — telling us
about it helps everyone.
