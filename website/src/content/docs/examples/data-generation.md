---
title: Generating Realistic Data That Respects Your Schema
description: Walk through SqlProof's dataset generator — FK-respecting, CHECK-aware, UNIQUE-honoring, with column overrides, derived values, external parent tables, and shrinkable cardinalities. Useful beyond tests.
---

Most of these docs talk about *property-based testing* — the part of
SqlProof where you write an invariant and the engine throws random data
at it. This page is about **the data engine itself**, which is useful
on its own:

- Seeding a local dev database with realistic relational data.
- Generating fixtures that respect FK / CHECK / UNIQUE / NOT NULL
  constraints without rejection sampling or hand-curated fixture
  files.
- Replaying schema-respecting datasets through migrations to verify
  they don't break under data shapes you haven't seen yet.
- Wiring an external parent table (e.g. Supabase `auth.users`) into
  generation so child rows draw FK values from real, existing parents.

Everything below uses the public `dataset_strategy` API that the
property runners are built on. You can use it directly, no `pytest`
required.

## The basic loop

```python
from sqlproof import SqlProof

proof = SqlProof.from_connection_string("postgresql://localhost/mydb")

# Compose a strategy that yields a full multi-table dataset.
strategy = proof.dataset_strategy(
    sizes={"customers": 50, "orders": 200, "line_items": 600},
)

# Draw one example. (For property tests, a runner draws hundreds.)
from hypothesis.strategies import SearchStrategy
dataset = strategy.example()
# {'customers': [{...} × 50], 'orders': [{...} × 200], 'line_items': [{...} × 600]}
```

The dataset's structure:

- **FK-respecting.** Every `orders.customer_id` points to a real
  `customers.id` from the same dataset. Every `line_items.order_id`
  points to a real `orders.id`. Tables are inserted in topological
  order so the parents exist when children reference them.
- **Type-respecting.** `NUMERIC(10,2)` columns get scale-2 decimals.
  `varchar(50)` gets strings under 50 characters. `uuid` gets parseable
  UUID strings.
- **NULL-respecting.** `NOT NULL` columns are never null; nullable
  columns can be null.
- **CHECK-respecting.** See below.

To actually load the dataset into a database:

```python
with proof.client_for_dataset(dataset) as db:
    # All tables are inserted, in FK order, into a savepoint.
    # Run any queries you want against db.query / db.execute.
    rows = db.query("SELECT customer_id, COUNT(*) FROM orders GROUP BY 1")
    print(rows)
# Savepoint is rolled back on context exit, leaving the DB clean.
```

## CHECK constraints are honored automatically

This is the part where SqlProof is doing real work for you. Given:

```sql
CREATE TABLE products (
    id        SERIAL PRIMARY KEY,
    sku       VARCHAR(20) NOT NULL,
    price     NUMERIC(10,2) NOT NULL CHECK (price >= 0 AND price < 10000),
    quantity  INTEGER NOT NULL CHECK (quantity > 0),
    status    TEXT NOT NULL CHECK (status IN ('draft', 'live', 'retired')),
    weight_kg NUMERIC(8,3) CHECK (weight_kg >= 0)
);
```

SqlProof's CHECK refiner reads each constraint and **adjusts the
generator** so values land in-domain *before* INSERT, instead of
generating something invalid and asking Postgres to reject it. The
following constraint shapes are recognized:

| Shape | Example | What the refiner does |
|---|---|---|
| Range | `price >= 0`, `quantity > 0`, `score < 100` | Narrows the numeric strategy's bounds |
| IN-set | `status IN ('draft', 'live', 'retired')` | Replaces with `sampled_from(...)` |
| ANY(ARRAY) | `priority = ANY(ARRAY[1, 2, 3, 5, 8])` | Same as IN-set |
| Length | `length(sku) <= 12`, `char_length(name) >= 3` | Narrows string-length bounds |

Constraints the refiner doesn't yet recognize (regex match, compound
boolean expressions, function calls) fall back to a permissive
strategy with a `.filter(...)` predicate when the expression has a
parseable arithmetic comparator, or to the unrefined strategy
otherwise. **Generated rows are still valid against type and FK
constraints, but Postgres may reject them on the unrecognized
CHECK** — which itself is a useful signal: it tells you which
constraints are exotic enough that you might want to express them
differently or override the column directly.

## Column overrides: fixed, strategy, or derived

Three flavors via the `columns=` parameter to `dataset_strategy`:

### Fixed value

```python
strategy = proof.dataset_strategy(
    sizes={"customers": 10},
    columns={"customers.email_domain": "test.invalid"},
)
# Every generated customer has email_domain = "test.invalid".
```

### Hypothesis strategy

```python
from hypothesis import strategies as st
from sqlproof.generators.well_known import emails

strategy = proof.dataset_strategy(
    sizes={"customers": 10, "orders": 30},
    columns={
        "customers.email":         emails(domains=["example.com"]),
        "customers.country_code":  st.sampled_from(["US", "GB", "DE", "JP", "CA"]),
        "orders.total":            st.decimals("0.01", "999.99", places=2),
    },
)
```

### Derived (callable)

The override receives a `ColumnContext` with the row being built so
far, and returns the derived value:

```python
strategy = proof.dataset_strategy(
    sizes={"line_items": 50},
    columns={
        # quantity and unit_price are generated by the schema strategies;
        # total is *derived* from them so the row is internally consistent.
        "line_items.total": lambda ctx: ctx.row["quantity"] * ctx.row["unit_price"],
    },
)
```

This is invaluable when CHECK constraints involve cross-column
relationships (`CHECK (total = quantity * unit_price)`). The CHECK
refiner can't infer such relationships from the expression text, but
a one-line derived override solves it.

## Shrinkable cardinalities

`sizes={"orders": 50}` always generates exactly 50. For property
tests, *shrinkable* sizes find smaller failing examples:

```python
sizes = {
    "customers": st.integers(min_value=1, max_value=20),
    "orders":    st.integers(min_value=0, max_value=100),
}
strategy = proof.dataset_strategy(sizes=sizes)
```

When a property fails on `customers=15, orders=87`, Hypothesis can
shrink to `customers=2, orders=3` if that still reproduces the
bug — usually it can. Smaller counterexamples are dramatically
easier to debug.

## External parent tables (Supabase, multi-tenant systems)

Sometimes a FK target is owned by an external system you can't
generate into — Supabase's `auth.users`, a tenant directory, a CRM
mirror. SqlProof's `ExternalTableSpec` lets the generator draw FK
values from a live external parent without trying to insert into it:

```python
from sqlproof import ExternalTableSpec, SqlProof
from hypothesis import strategies as st

def sample_test_users(db):
    rows = db.query(
        "SELECT id FROM auth.users WHERE email LIKE 'test_%@example.test'"
    )
    return [row["id"] for row in rows]

proof = SqlProof.from_connection_string(
    "postgresql://...",
    external_tables={
        "auth.users": ExternalTableSpec(
            primary_key="id",
            seed_count=st.integers(min_value=1, max_value=5),
            sample=sample_test_users,
        ),
    },
)

# Now `projects.user_id REFERENCES auth.users(id)` will pick from
# the live external user pool instead of trying to generate into auth.users.
strategy = proof.dataset_strategy(sizes={"projects": 10})
```

For Supabase specifically, the `sqlproof.contrib.supabase` module
ships two helpers that make this turnkey:
[testing Supabase apps guide](/guides/supabase/).

## Composite UNIQUE constraints

`UNIQUE (project_id, user_id)` on a `project_members` table is honored
during generation — duplicates are rejected and replaced before INSERT.
You don't have to do anything; if the schema has it, the generator
respects it.

```sql
CREATE TABLE project_members (
    project_id uuid REFERENCES projects(id),
    user_id    uuid REFERENCES users(id),
    role       text NOT NULL,
    UNIQUE (project_id, user_id)
);
```

Generated `project_members` rows will never contain two rows with the
same `(project_id, user_id)` pair, even though they're separately
sampled per row.

## Use case: seeding a local dev database

Same machinery, no `pytest` involved. Useful when starting a fresh
environment or after a `db reset`:

```python
# scripts/seed_dev_db.py
import os
from sqlproof import SqlProof

proof = SqlProof.from_connection_string(os.environ["DATABASE_URL"])
strategy = proof.dataset_strategy(sizes={
    "users":         50,
    "projects":      20,
    "tasks":         300,
    "comments":      800,
    "audit_events": 2000,
})

dataset = strategy.example()

# `client_for_dataset` rolls back by default. To commit:
from sqlproof.client import PsycopgSqlProofClient
import psycopg
from psycopg.rows import dict_row
from sqlproof.core import _insert_dataset

with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True,
                     row_factory=dict_row) as conn:
    client = PsycopgSqlProofClient(conn)
    _insert_dataset(client, proof.schema_info, dataset)

print(f"Seeded {sum(len(rows) for rows in dataset.values())} rows.")
```

## Use case: migration safety against generated data

Run a candidate migration against many generated datasets to verify it
doesn't lose information or change query results:

```python
def test_new_index_does_not_change_query_results(proof):
    @sqlproof(proof, sizes={"orders": 100, "line_items": 500}, runs=50)
    def queries_match(db):
        before = db.query("SELECT * FROM order_summary_view ORDER BY order_id")
        db.execute("CREATE INDEX idx_orders_customer ON orders(customer_id)")
        after = db.query("SELECT * FROM order_summary_view ORDER BY order_id")
        assert before == after
    queries_match()
```

The generator produces a different valid dataset on every iteration;
the index is created and the view is re-queried each time. If a
PG-specific quirk causes the index to change query plan in a way that
*also* changes results (a real risk for queries with `LIMIT` or window
functions and no explicit `ORDER BY`), the property fails on a minimal
counterexample.

## What the generator doesn't (yet) do

Honest limitations, all tracked openly:

- **Range types, composite types, custom domains.** [#4 on the
  issue tracker](https://github.com/alialavia/sqlproof/issues/4).
  Currently fall back to a text strategy.
- **Exclusion constraints, partial unique indexes, generated
  columns.** [#3](https://github.com/alialavia/sqlproof/issues/3).
  Generated columns in particular need a fix soon — the generator
  currently tries to populate them and Postgres rejects the row.
- **CHECK constraints involving regex or complex boolean expressions.**
  Falls through to the unrefined strategy (with a `.filter(...)` if a
  numeric comparator is buried in there). Override the column
  manually if this matters.
- **Cross-row relational invariants** that aren't expressed as schema
  constraints — e.g. "every order must have at least one line item."
  Use a derived size strategy or a column override that ensures the
  relationship.

For everything else: if it's expressed as a Postgres-recognized
schema constraint, the generator should honor it. If it doesn't,
that's a bug — please [open an issue](https://github.com/alialavia/sqlproof/issues).
