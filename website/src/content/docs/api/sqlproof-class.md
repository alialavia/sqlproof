---
title: SqlProof Class
description: The main Python class for configuring schemas and running properties.
---

`SqlProof` is the entry point for a test suite. Create one instance from a schema file,
connection string, or config object, then use `@sqlproof`, `check()`, or `invariant()`.

## Constructors

```python
from sqlproof import SqlProof

proof = SqlProof.from_schema_file("./schema.sql")
proof = SqlProof.from_connection_string("postgresql://localhost:5432/testdb")
```

For explicit configuration:

```python
from sqlproof import SqlProof, SqlProofConfig

proof = SqlProof.from_config(
    SqlProofConfig(
        schema_file="./schema.sql",
        image="postgres:16",
        reuse_container=True,
    )
)
```

Exactly one of `schema_file` or `connection_string` must be provided.

## Decorator Usage

```python
from sqlproof import SqlProof, sqlproof

proof = SqlProof.from_schema_file("./schema.sql")


@sqlproof(proof, sizes={"customers": 10, "orders": 50}, runs=50)
def test_order_totals_non_negative(db):
    rows = db.query("SELECT total FROM orders")
    assert all(row["total"] >= 0 for row in rows)
```

## Imperative Usage

```python
def check_totals(db):
    rows = db.query("SELECT total FROM orders")
    assert all(row["total"] >= 0 for row in rows)


proof.check(
    name="order totals are non-negative",
    sizes={"customers": 10, "orders": 50},
    property=check_totals,
    runs=100,
)
```

## Invariants

Use `invariant()` when a SQL query should return zero rows.

```python
proof.invariant(
    name="no orphan orders",
    sizes={"customers": 10, "orders": 50},
    query="""
        SELECT o.id
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        WHERE c.id IS NULL
    """,
    expect_empty=True,
)
```

## External Tables

When your schema has FKs into a table SqlProof shouldn't generate (e.g.
Supabase's `auth.users`), register an `ExternalTableSpec` so the generator
samples FK values from the live external parent:

```python
from sqlproof import ExternalTableSpec, SqlProof
from hypothesis import strategies as st

proof = SqlProof.from_connection_string(
    "postgresql://...",
    external_tables={
        "auth.users": ExternalTableSpec(
            primary_key="id",
            seed_count=st.integers(min_value=1, max_value=5),
            sample=lambda db: [r["id"] for r in db.query(
                "SELECT id FROM auth.users WHERE email LIKE 'sqlproof_%%'"
            )],
        )
    },
)
```

See the [Supabase guide](/guides/supabase/) for a full walkthrough.

## Stateful tests

For invariants that only surface across sequences of mutations
(pagination, windowed aggregates, RLS membership churn), use a state
machine via `proof.run_state_machine`:

```python
from sqlproof.testing import SqlProofStateMachine
from hypothesis.stateful import rule, invariant

class MyMachine(SqlProofStateMachine):
    @rule()
    def step(self): ...

    @invariant()
    def some_invariant(self): ...

def test_x(proof: SqlProof):
    proof.run_state_machine(MyMachine)
```

See the [stateful testing API](/api/state-machine/) for details.

## Cleanup

`SqlProof` is a context manager:

```python
with SqlProof.from_schema_file("./schema.sql") as proof:
    proof.invariant(...)
```
