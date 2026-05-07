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

## Project setup

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

### Tell SqlProof where the database is

```bash
export SUPABASE_DB_URL='postgresql://postgres:postgres@127.0.0.1:54322/postgres'
```

**There is no `tests/conftest.py` to write.** SqlProof's pytest plugin
ships these fixtures out of the box:

- `proof` (session) — a `SqlProof` connected to `SUPABASE_DB_URL`.
- `db` (per-test) — a `SqlProofClient` with savepoint isolation.
- `supabase_proof` (session) — like `proof`, but with the deterministic
  `auth.users` test pool seeded and registered as an external table for
  FK draws. **Use this for any test that touches RLS, `auth.uid()`,
  or RPCs that key off auth users.**
- `supabase_db` (per-test) — `SqlProofClient` backed by `supabase_proof`.

If a test doesn't touch auth, take `db`. If it does, take `supabase_db`.
Don't define `proof` / `db` yourself unless you need a custom external
table beyond the auth-users pool.

### Running tests

```bash
pytest tests/ -v
```

Each test runs inside a savepoint that rolls back when the test ends.
Tests cannot leak data into the local Supabase.

---

## Property tests over hand-rolled fixtures (the core idiom)

The whole reason to use SqlProof over pgTAP is that it generates *many
valid datasets* for a single test, so edge cases surface that you'd
never think to type. Concretely, this means:

**Don't write `_insert_user`, `_insert_project`, `_insert_event` helpers
in your tests.** Hand-rolled INSERTs test only the shape *you*
remembered, not the shape your schema actually permits. They're the
pgTAP-shaped pattern; they miss the entire point of SqlProof.

**Do use `dataset_strategy` to generate, then assert.** The pattern:

```python
from hypothesis import given
from hypothesis import strategies as st


@given(data=st.data())
def test_my_invariant(supabase_proof, data):
    dataset = data.draw(supabase_proof.dataset_strategy(
        sizes={"projects": 1, "events": 5},
        # auth.users come from the seeded test-user pool via FK
    ))
    with supabase_proof.client_for_dataset(dataset) as db:
        # ... assertion against the generated dataset
```

Each example generates a fresh dataset that respects every FK, CHECK,
UNIQUE, and NOT NULL in your schema. If you write a hand-rolled INSERT
helper instead, you're regressing to pgTAP behavior and getting none of
SqlProof's benefit.

There are exactly two cases where hand-rolled INSERTs are acceptable:

1. **A test that needs a *specific*, fixed shape** — e.g. asserting an
   empty-state contract returns a structurally complete zero-payload
   for an unknown ID. Use a single literal `00000000-...-000000000000`
   UUID and don't generate.
2. **A small helper inside a stateful test machine** that mutates rows
   between rules. Even there, prefer `proof.client_for_dataset(...)`
   to seed the initial state.

---

## Pattern 1: RLS policy test

**When to write:** any time the user adds or modifies a `CREATE POLICY` statement.

**Template:**

```python
"""Test that RLS policies on `<table>` correctly gate access."""

from hypothesis import given
from hypothesis import strategies as st

from sqlproof import SqlProof
from sqlproof.contrib.supabase import as_supabase_user


@given(data=st.data())
def test_owner_can_read_their_own_<resource>(
    supabase_proof: SqlProof, data,
) -> None:
    dataset = data.draw(supabase_proof.dataset_strategy(
        sizes={"<resource_table>": 1},
    ))
    with supabase_proof.client_for_dataset(dataset) as db:
        resource = dataset["<resource_table>"][0]
        with as_supabase_user(db, resource["user_id"]):
            rows = db.query(
                "SELECT id FROM <resource_table> WHERE id = %s",
                resource["id"],
            )
        assert len(rows) == 1, "owner should see their own resource"


@given(data=st.data())
def test_other_users_cannot_read_<resource>_they_dont_own(
    supabase_proof: SqlProof, data,
) -> None:
    dataset = data.draw(supabase_proof.dataset_strategy(
        sizes={"<resource_table>": 1, "auth.users": 2},
    ))
    with supabase_proof.client_for_dataset(dataset) as db:
        resource = dataset["<resource_table>"][0]
        non_owner = next(
            u for u in dataset["auth.users"] if u["id"] != resource["user_id"]
        )
        with as_supabase_user(db, non_owner["id"]):
            rows = db.query(
                "SELECT id FROM <resource_table> WHERE id = %s",
                resource["id"],
            )
        assert rows == [], "non-owner should see no rows"
```

**Important rules:**
- **Always use `as_supabase_user(db, user_id)`** to set RLS context. Do not raw-set `request.jwt.claims`.
- **Always test both directions:** owner can access, non-owner cannot. A policy that returns *too much* data is the actual bug class.
- **Take `supabase_proof`/`supabase_db`**, not `proof`/`db`. RLS tests need the seeded auth.users pool.
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

**For functions that aggregate over a generated dataset**, generate the
dataset and reconcile against a Python recomputation:

```python
@given(data=st.data(), event_count=st.integers(min_value=0, max_value=20))
def test_dashboard_summary_event_count_matches_inserted_count(
    supabase_proof, data, event_count,
):
    dataset = data.draw(supabase_proof.dataset_strategy(
        sizes={"projects": 1, "events": event_count},
    ))
    with supabase_proof.client_for_dataset(dataset) as db:
        project_id = dataset["projects"][0]["id"]
        payload = db.scalar(
            "SELECT get_dashboard_summary(%s::uuid)", project_id
        )
    assert payload["event_count"] == event_count
```

**For empty-state behavior**, use a single example test with a literal
unknown UUID:

```python
def test_dashboard_summary_returns_zero_shape_for_unknown_project(db):
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
- **Don't hand-roll the dataset.** Use `dataset_strategy` even when you "just need one row" — that one row should still respect every constraint.

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
        # Pull a real user from the seeded auth.users pool.
        rows = self.db.query(
            r"SELECT id::text FROM auth.users WHERE email LIKE %s ESCAPE '\\' LIMIT 4",
            r"sqlproof\\_%@test.invalid",
        )
        self.user_id = rows[0]["id"]
        # Generate three projects belonging to the other test users so
        # we can exercise membership transitions between them.
        # ... (`proof.dataset_strategy(...)` per machine if you need
        # generated state; otherwise insert a literal handful here)
        self.projects: list[str] = [str(uuid4()) for _ in range(3)]
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


def test_membership_visibility_invariant(supabase_proof: SqlProof) -> None:
    supabase_proof.run_state_machine(MembershipMachine)
```

**Important rules:**
- **Override `on_setup`, not `__init__`.** SqlProof manages `__init__`.
- **Use `self.enter(cm)` for context managers** that should live across rules (JWT claims, savepoints).
- **Run with `supabase_proof.run_state_machine(MachineClass)`**, not `run_state_machine_as_test` directly.
- **State machines are slower than property tests.** Use them only when the bug requires a sequence.

---

## Anti-patterns: things agents commonly get wrong

### ❌ Don't write hand-rolled `_insert_user` / `_insert_project` helpers

```python
# Wrong — defeats the entire point of property-based testing:
def test_x(db):
    owner_id = _insert_user(db)
    project_id = _insert_project(db, owner_id)
    _insert_events(db, project_id, count=3)
    ...
```

```python
# Right — let SqlProof generate, including respecting all constraints:
@given(data=st.data())
def test_x(supabase_proof, data):
    dataset = data.draw(supabase_proof.dataset_strategy(
        sizes={"projects": 1, "events": 3},
    ))
    with supabase_proof.client_for_dataset(dataset) as db:
        ...
```

The hand-rolled helpers test only the shape *you* hand-built. The
generated approach tests every shape your schema permits.

### ❌ Don't write a `tests/conftest.py` with `proof` / `db` fixtures

The plugin ships them. If you find yourself copying ~30 lines of fixture
boilerplate into a project, you have an out-of-date docs page. The
correct setup is one `export SUPABASE_DB_URL=...` line.

The only reason to define `proof` (or `supabase_proof`) yourself is if
your schema needs an *additional* external table beyond `auth.users` —
and that override is ~10 lines, not 30.

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

If `seed_test_users_directly` fails, the connection is using a non-postgres role. Use `supabase_proof` (which seeds once per session) and sample from the pool via the `auth.users` external table the fixture registers — don't re-seed per test.

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
│   ├── test_rls_<table>.py            # one file per table with RLS policies
│   ├── test_rpc_<function_name>.py    # one file per public function
│   ├── test_trigger_<trigger_name>.py # one file per trigger
│   └── test_migration_<n>_<desc>.py   # migration safety tests
└── supabase/
    ├── migrations/...
    ├── schemas/...
    └── tests/...                      # pgTAP files (separate suite, leave alone)
```

A `tests/conftest.py` is **optional** — only needed if you're
overriding `proof` or `supabase_proof` for a custom external table.

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
