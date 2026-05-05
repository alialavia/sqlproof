# SqlProof — Agent Rules for Testing Supabase Projects

> If you are an AI coding agent reading this file, follow the patterns below
> whenever you write tests for a Supabase-shaped project that uses SqlProof.
> These rules are tuned for solo founders who don't write tests by hand —
> the human reads test names and failure messages, not implementations.
>
> Keep tests **short, named like sentences, and focused on one behavior**.

---

## When to use SqlProof

Use SqlProof for any Supabase project test that touches:

- **RLS policies** — verify that a user can only see / modify rows they should.
- **`public.*` SQL functions / RPCs** — verify what the function returns for given inputs.
- **Triggers** — verify the side effect a trigger produces on a parent table change.
- **Migrations** — verify the new query produces the same answer as the old query for any input dataset.
- **Aggregates and reports** — verify the database's aggregate matches a Python recomputation.

Do **not** use SqlProof for:
- Schema-shape assertions ("does this column exist") — write a one-line `information_schema` query in pgTAP or skip entirely.
- Snapshot tests of literal output — use `syrupy` or `pytest`'s built-in snapshot.

## Project setup (do this exactly once)

The user runs Supabase locally via `supabase start`. The local DB is at
`postgresql://postgres:postgres@127.0.0.1:54322/postgres`. Tests run from
the project root, not from inside `supabase/`.

### `pyproject.toml` (or equivalent dependency declaration)

```toml
[tool.pytest.ini_options]
addopts = "-ra"
testpaths = ["tests"]

[project.optional-dependencies]
dev = [
  "sqlproof",
  "pytest>=8",
  "hypothesis>=6.100",
  "psycopg[binary]>=3.1",
]
```

Install with `pip install --pre -e ".[dev]"` (sqlproof is alpha; the `--pre`
flag is required until the first stable release).

### `tests/conftest.py`

Create this file once. Do not regenerate it on subsequent test additions.

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
    """SqlProof instance bound to the local Supabase database."""
    proof = SqlProof.from_connection_string(DATABASE_URL)
    try:
        # Seed a deterministic pool of test users for FK sampling on auth.users.
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
    """Per-test database client. Inserts roll back automatically."""
    with proof.client_for_dataset({}) as client:
        yield client
```

### Running tests

```bash
pytest tests/ -v
```

Each test runs inside a savepoint that rolls back when the test ends.
Tests cannot leak data into the local Supabase.

---

## Pattern 1: RLS policy test

**When to write:** any time the user adds or modifies a `CREATE POLICY` statement.

**Template:**

```python
"""Test that RLS policies on `<table>` correctly gate access."""

from uuid import uuid4
from sqlproof.contrib.supabase import as_supabase_user
from sqlproof.client import SqlProofClient


def test_owner_can_read_their_own_<resource>(db: SqlProofClient) -> None:
    owner_id = _insert_user(db)
    other_id = _insert_user(db)
    resource_id = _insert_<resource>(db, owner_id=owner_id)

    with as_supabase_user(db, owner_id):
        rows = db.query("SELECT id FROM <table> WHERE id = %s", resource_id)

    assert len(rows) == 1, "owner should see their own resource"


def test_other_users_cannot_read_<resource>_they_dont_own(db: SqlProofClient) -> None:
    owner_id = _insert_user(db)
    other_id = _insert_user(db)
    resource_id = _insert_<resource>(db, owner_id=owner_id)

    with as_supabase_user(db, other_id):
        rows = db.query("SELECT id FROM <table> WHERE id = %s", resource_id)

    assert rows == [], "non-owner should see no rows"


def _insert_user(db: SqlProofClient) -> str:
    user_id = str(uuid4())
    db.execute(
        "INSERT INTO auth.users (id, aud, role, email) "
        "VALUES (%s, 'authenticated', 'authenticated', %s)",
        user_id, f"{user_id}@test.invalid",
    )
    return user_id


def _insert_<resource>(db: SqlProofClient, owner_id: str) -> str:
    resource_id = str(uuid4())
    db.execute(
        "INSERT INTO <table> (id, user_id, ...) VALUES (%s, %s, ...)",
        resource_id, owner_id,
    )
    return resource_id
```

**Important rules:**
- **Always use `as_supabase_user(db, user_id)`** to set RLS context. Do not raw-set `request.jwt.claims`.
- **Always test both directions:** owner can access, non-owner cannot. A policy that returns *too much* data is the actual bug class.
- **Test names should be sentences a non-engineer would understand.** `test_owner_can_read_their_own_project` not `test_rls_22`.

## Pattern 2: SQL function / RPC test

**When to write:** any time the user adds or modifies a `CREATE FUNCTION` in `public.*`.

**For deterministic functions with simple inputs**, use a property test:

```python
"""Property tests for `compute_order_total`."""

from decimal import Decimal
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlproof.client import SqlProofClient

PROOF_KW = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@PROOF_KW
@given(
    subtotal=st.decimals(min_value=Decimal("0"), max_value=Decimal("9999.99"), places=2),
    tier=st.sampled_from(["standard", "silver", "gold", "platinum"]),
)
def test_invoice_total_is_never_negative(db: SqlProofClient, subtotal, tier):
    result = db.scalar(
        "SELECT compute_order_total(%s::numeric, %s)",
        subtotal, tier,
    )
    assert result >= 0


@PROOF_KW
@given(
    subtotal=st.decimals(min_value=Decimal("1"), max_value=Decimal("1000"), places=2),
)
def test_higher_tier_never_costs_more_than_lower_tier(db: SqlProofClient, subtotal):
    standard = db.scalar("SELECT compute_order_total(%s, 'standard')", subtotal)
    platinum = db.scalar("SELECT compute_order_total(%s, 'platinum')", subtotal)
    assert platinum <= standard, f"platinum ({platinum}) costs more than standard ({standard})"
```

**For empty-state behavior**, use a single example test:

```python
def test_dashboard_summary_returns_zero_shape_for_unknown_project(db: SqlProofClient):
    payload = db.scalar(
        "SELECT get_dashboard_summary(%s::uuid)",
        "00000000-0000-0000-0000-000000000000",
    )
    assert payload["totalUsers"] == 0
    assert payload["recentEvents"] == []
```

**Important rules:**
- **Property tests describe an invariant.** "X is never negative." "Higher tier never costs more." "Sum of X equals sum of Y."
- **Use `db.scalar(...)` for functions returning a single value**; `db.query(...)` for `RETURNS TABLE`.
- **Cast inputs explicitly:** `%s::uuid`, `%s::numeric`, `%s::text[]`. Postgres's parameter-binding is strict.
- **Do not invent properties.** If you can't articulate the invariant in one sentence, write an example test instead.

## Pattern 3: Stateful test (RLS membership churn, pagination, accumulation)

**When to write:** when the bug class is "works first time, fails after a sequence of operations."

```python
"""Stateful test: project membership and visibility."""

from uuid import uuid4
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import invariant, rule

from sqlproof import SqlProof
from sqlproof.contrib.supabase import as_supabase_user
from sqlproof.testing import SqlProofStateMachine


class MembershipMachine(SqlProofStateMachine):
    def on_setup(self) -> None:
        self.user_id = self._insert_user()
        self.projects = [self._insert_project() for _ in range(3)]
        self.enter(as_supabase_user(self.db, self.user_id))
        self.member_of: set[str] = set()

    @rule(idx=st.integers(0, 2))
    def join_project(self, idx):
        project_id = self.projects[idx]
        self.db.execute(
            "INSERT INTO project_members (project_id, user_id, role) "
            "VALUES (%s, %s, 'viewer') ON CONFLICT DO NOTHING",
            project_id, self.user_id,
        )
        self.member_of.add(project_id)

    @rule(idx=st.integers(0, 2))
    def leave_project(self, idx):
        project_id = self.projects[idx]
        self.db.execute(
            "DELETE FROM project_members WHERE project_id = %s AND user_id = %s",
            project_id, self.user_id,
        )
        self.member_of.discard(project_id)

    @invariant()
    def user_only_sees_projects_they_are_member_of(self) -> None:
        visible = {row["id"] for row in self.db.query("SELECT id FROM projects")}
        assert visible == self.member_of, (
            f"visible {visible} != expected {self.member_of}"
        )

    def _insert_user(self) -> str:
        user_id = str(uuid4())
        self.db.execute(
            "INSERT INTO auth.users (id, aud, role, email) "
            "VALUES (%s, 'authenticated', 'authenticated', %s)",
            user_id, f"{user_id}@test.invalid",
        )
        return user_id

    def _insert_project(self) -> str:
        project_id = str(uuid4())
        self.db.execute(
            "INSERT INTO projects (id, owner_id, name) VALUES (%s, %s, %s)",
            project_id, str(uuid4()), "Test",
        )
        return project_id


def test_membership_visibility_invariant(proof: SqlProof) -> None:
    proof.run_state_machine(MembershipMachine)
```

**Important rules:**
- **Override `on_setup`, not `__init__`.** SqlProof manages `__init__`.
- **Use `self.enter(cm)` for context managers** that should live across rules (JWT claims, savepoints).
- **Run with `proof.run_state_machine(MachineClass)`**, not `run_state_machine_as_test` directly.
- **State machines are slower than property tests.** Use them only when the bug requires a sequence.

---

## Anti-patterns: things agents commonly get wrong

### ❌ Don't manually set JWT claims

```python
# Wrong — leaks Postgres internals into the test:
db.execute(
    "SELECT set_config('request.jwt.claims', %s, true)",
    json.dumps({"sub": user_id, "role": "authenticated"}),
)
```

```python
# Right — readable, restored on exit, exception-safe:
with as_supabase_user(db, user_id):
    ...
```

### ❌ Don't insert into `auth.users` if the test connection lacks permission

If `seed_test_users_directly` fails, the connection is using a non-postgres role. Use the deterministic pool seeded once in `conftest.py` rather than inserting fresh users per test.

### ❌ Don't use `pytest.fixture` for per-test data setup when generation can do it

```python
# Wrong — hand-rolled fixture, doesn't scale, doesn't shrink:
@pytest.fixture
def project_with_three_orders(db):
    project_id = db.execute("INSERT INTO projects ...")
    for _ in range(3):
        db.execute("INSERT INTO orders ...")
    return project_id
```

```python
# Right — let SqlProof generate the dataset, including FK respect:
def test_project_revenue_calculation(db, proof):
    dataset = proof.dataset_strategy(
        sizes={"projects": 1, "orders": 3},
        columns={"projects.name": "Test"},
    ).example()
    with proof.client_for_dataset(dataset) as scoped:
        # ... assertions against scoped
```

### ❌ Don't write tests that depend on existing data in the local Supabase

Tests should set up everything they need. The `db` fixture is empty per-test (rolled back). Don't query for "the user named X" expecting them to exist.

### ❌ Don't skip the `:: cast` in raw SQL

```python
db.scalar("SELECT my_function(%s, %s)", uuid_value, integer_value)  # ambiguous
```

```python
db.scalar("SELECT my_function(%s::uuid, %s::int)", uuid_value, integer_value)  # explicit
```

Postgres function overload resolution requires explicit casts when the
parameter types aren't obvious from the literal.

### ❌ Don't run state machines for one-shot assertions

A state machine has setup overhead per example. If your assertion doesn't depend on a *sequence* of operations, write a property test (`@given`) instead.

---

## File and naming conventions

```
project_root/
├── tests/
│   ├── conftest.py                    # the one from above
│   ├── test_rls_<table>.py            # one file per table with RLS policies
│   ├── test_rpc_<function_name>.py    # one file per public function
│   ├── test_trigger_<trigger_name>.py # one file per trigger
│   └── test_migration_<n>_<desc>.py   # migration safety tests
└── supabase/
    ├── migrations/...
    ├── schemas/...
    └── tests/...                      # pgTAP files (separate suite, leave alone)
```

**Test name shape:** `test_<subject>_<expected_behavior>_<conditions>`

Good:
- `test_owner_can_read_their_own_project`
- `test_non_owner_cannot_modify_shared_project`
- `test_get_dashboard_summary_returns_zero_for_unknown_project`
- `test_higher_tier_never_costs_more_than_lower_tier`

Bad:
- `test_policy_22`
- `test_function_works`
- `test_select_query`

The user reads test names in failure summaries. Make them sentences.

---

## When the agent is unsure

If the user asks for a test you don't have a pattern for, ask **one
clarifying question** about the intended invariant. Don't guess and ship
a test that asserts the wrong thing. Specifically:

- "What should this function return when the input is empty/null/zero?"
- "Should this RLS policy hide rows for unauthenticated users, or just non-owners?"
- "Is this aggregation expected to round halfway-cases up, down, or to-even?"

Once the invariant is clear, write the test. If still uncertain after one
question, write the smallest possible example test and flag the area for
the user to expand.
