# SqlProof — Agent Rules for Testing Supabase Projects

> If you are an AI coding agent reading this file, follow the patterns below
> whenever you write tests for a Supabase-shaped project that uses SqlProof.
> These rules are tuned for solo founders who don't write tests by hand —
> the human reads test names and failure messages, not implementations.
>
> Keep tests **short, named like sentences, and focused on one behavior**.

---

## Before continuing: install the two plugins this file builds on

This file is **sqlproof-specific**. The patterns below assume you
already have:

1. **Foundational property-based-testing patterns** — picking real
   invariants, naming oracles, avoiding tautology traps. Lives in
   the [`alialavia/pbt-skills`](https://github.com/alialavia/pbt-skills)
   plugin.
2. **Per-pattern sqlproof guides** — focused, auto-activating skills
   for RLS testing, RPC testing, stateful testing, and CI setup.
   Lives in the
   [`alialavia/sqlproof-skills`](https://github.com/alialavia/sqlproof-skills)
   plugin. Each skill is ~200 lines and loads only when the task
   triggers it — versus this 700-line file which loads in full.

**If you're Claude Code:**

```
/plugin install alialavia/pbt-skills
/plugin install alialavia/sqlproof-skills
```

(If this project ships a `.claude/settings.json` — it does — Claude
Code will prompt you to install both declared plugins on your first
session here. Accept.)

**If you're Codex / Cursor / Aider / any other agent without a plugin
system:** fetch the SKILL.md files at
<https://github.com/alialavia/pbt-skills/tree/main/skills> and
<https://github.com/alialavia/sqlproof-skills/tree/main/skills>
and load them into your context before continuing.

Without `pbt-skills` (or its content in your context), the tests you
write against the patterns below will tend to be tautological,
self-oracle, or property-shaped-but-not-property-based. Without
`sqlproof-skills`, you'll be reading this entire file every session
when a focused per-pattern skill would be sufficient.

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

---

## "Add SqlProof to my repo" — the bootstrap

If the user just asked you to set up SqlProof in their project from
scratch, do these five steps in order. Each is one PR-sized change; you
can ship them together if the user wants.

### 1. Install + declare the dev dependency

```toml
# pyproject.toml
[project.optional-dependencies]
dev = [
  "sqlproof",
  "pytest>=8",
  "hypothesis>=6.100",
  "psycopg[binary]>=3.1",
]

[tool.pytest.ini_options]
addopts = "-ra"
testpaths = ["tests"]
```

Then `pip install -e ".[dev]"` (or `uv sync --extra dev`).

### 2. Tell SqlProof where the DB is

For local development with `supabase start`:

```bash
export SUPABASE_DB_URL='postgresql://postgres:postgres@127.0.0.1:54322/postgres'
```

The pytest plugin reads `--sqlproof-database-url` → `$SQLPROOF_DATABASE_URL`
→ `$SUPABASE_DB_URL` (in that order). Without any of these, sqlproof's
fixtures skip cleanly — they don't fail.

### 3. Copy this `AGENTS.md` and the `.claude/settings.json` into the user's repo

```bash
# In the user's project root
curl -O https://raw.githubusercontent.com/alialavia/sqlproof/main/AGENTS.md
mkdir -p .claude
cat > .claude/settings.json <<'JSON'
{
  "plugins": [
    "alialavia/pbt-skills",
    "alialavia/sqlproof-skills"
  ]
}
JSON
```

The `AGENTS.md` is what gives future agent sessions the patterns. The
`.claude/settings.json` is what gets Claude Code users to auto-load the
foundational PBT skills.

### 4. Write the first test

The user's most common ask is "write a test for this RLS policy" or
"write a test for this RPC." Don't ship a placeholder; pick the
shortest real test they'd benefit from based on what their schema looks
like. See [Pattern 1](#pattern-1-rls-policy-test) and
[Pattern 2](#pattern-2-sql-function--rpc-test) below.

### 5. Wire it into CI

See [CI/CD integration](#cicd-integration) below for a copy-paste
GitHub Actions workflow. Most Supabase projects benefit from this on
day one because RLS regressions don't show up in local dev — only
under the test user pool that CI exercises.

---

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

Install with `pip install -e ".[dev]"`.

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

## CI/CD integration

Most projects want sqlproof tests in CI on every PR. The setup is one
GitHub Actions workflow file. Drop this into `.github/workflows/test.yml`
in the user's repo:

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
      - run: pip install -e ".[dev]"
      - run: pytest
```

**What each piece does:**

- **`supabase/postgres:15.8.1.040`** (not `postgres:16`) — provides the
  `auth` schema with `auth.users`, the `plpgsql_check` extension, and
  the rest of the extensions Supabase bundles.
- **`alialavia/sqlproof/.github/actions/setup-supabase-test-db@main`** —
  a composite action that installs `plpgsql_check` and applies
  [GoTrue's auth migration](https://github.com/supabase/auth/blob/master/migrations/20220224000811_update_auth_functions.up.sql)
  so `auth.uid()` accepts both the legacy singular GUC and the modern
  JSON `request.jwt.claims` GUC. **Without this step**, `auth.uid()`
  silently returns NULL in tests and every RLS test passes for the
  wrong reason.
- **`SQLPROOF_DATABASE_URL`** at the job level — read by sqlproof's
  pytest plugin. Job-level scope means every step sees it.

In production, replace `@main` with a tagged release (e.g. `@v0.2.1`)
so upstream changes can't surprise the build.

**Variations:**

- **Multi-version Python matrix**: add `strategy: { matrix: { python-version: ["3.11", "3.12", "3.13"] } }` under `jobs.test`. The service container boots once per matrix job.
- **Local dev with the Supabase CLI**: use `SUPABASE_DB_URL` env var instead of `SQLPROOF_DATABASE_URL` so the same variable works locally and in CI (`supabase start` exposes Postgres on `127.0.0.1:54322`).
- **Vanilla Postgres (no Supabase auth needed)**: swap `supabase/postgres` for `postgres:16` and drop the `setup-supabase-test-db` step.

See the [full CI/CD guide](https://sqlproof.com/guides/ci-cd/) for the
extended version with troubleshooting.

---

## Property tests over hand-rolled fixtures (the core idiom)

**Why** property tests beat hand-rolled fixtures is the foundational
PBT teaching covered in `alialavia/pbt-skills` (the
`property-based-testing` skill, sections on tautologies, self-oracles,
and flat generators). Load that first if you haven't.

**How** that translates into sqlproof's API:

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

**Residual case — RLS regression pins, HTTP-layer tests, shared
fixtures that need one specific row.** Property tests fit most cases
but not all. When you genuinely need a single hand-pinned row, reach
for `proof.row_strategy(table, **overrides)` instead of writing the
INSERT yourself:

```python
# Schema-aware single-row builder. When a migration adds a NOT NULL
# column to `projects`, this keeps working — the generator fills it.
@pytest.fixture
def project(proof, db, user):
    row = proof.row_strategy("projects", user_id=user["id"]).example()
    db.execute(
        f"INSERT INTO projects ({', '.join(row)}) VALUES "
        f"({', '.join(['%s'] * len(row))})",
        *row.values(),
    )
    return row
```

`row_strategy` is a Hypothesis `SearchStrategy[dict]` — inside a
`@given`-decorated test, draw from it instead of calling `.example()`.
Override kwargs accept bare values, strategies, or callables. Unknown
column names raise immediately so typos surface at the call site.

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

### ❌ Don't pass parameters to `db.execute` / `db.query` as a list

`SqlProofClient`'s methods take `*params` (splat), not a single sequence
argument:

```python
# Wrong — sends ONE parameter (the list) to a query with TWO placeholders:
db.execute(
    "INSERT INTO posts (org_id, author_id) VALUES (%s, %s)",
    [org_id, author_id],
)
# psycopg.errors.SyntaxError: the query has 2 placeholders but 1 parameters
```

```python
# Right — each value is a separate positional arg:
db.execute(
    "INSERT INTO posts (org_id, author_id) VALUES (%s, %s)",
    org_id,
    author_id,
)
```

Same rule for `db.query(...)` and `db.scalar(...)`. The list-wrapping
pattern is what `psycopg.Cursor.execute` expects, but `SqlProofClient`
wraps psycopg with a splat-style signature. Easy to get wrong because
the failure mode is a confusing parameter-count mismatch.

### ❌ Don't access default-bearing columns without putting them in `columns={...}`

The dataset generator **omits columns with database defaults** from
the returned dataset (because the DB fills them in via the DEFAULT
clause; sqlproof doesn't need to provide a value at INSERT time). If
your test needs to *read* that column on the generated rows, you must
explicitly request it:

```python
# Schema: `posts.is_premium BOOLEAN NOT NULL DEFAULT false`

# Wrong — `is_premium` is missing from the dataset, KeyError at runtime:
@sqlproof(
    proof,
    sizes={"posts": 5},
    columns={
        "posts.status": st.sampled_from(["draft", "published"]),
    },
)
def test_visibility(db, dataset):
    for post in dataset["posts"]:
        expected = visible_to(post, role)  # reads post["is_premium"]
```

```python
# Right — explicitly request the default-bearing column:
@sqlproof(
    proof,
    sizes={"posts": 5},
    columns={
        "posts.status": st.sampled_from(["draft", "published"]),
        "posts.is_premium": st.booleans(),  # ← now in the dataset
    },
)
def test_visibility(db, dataset):
    for post in dataset["posts"]:
        expected = visible_to(post, role)  # works
```

Rule of thumb: if your test asserts anything about a column's value or
branches on it, that column belongs in `columns={...}`.

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
