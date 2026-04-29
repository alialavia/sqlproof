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

## Cleanup

`SqlProof` is a context manager:

```python
with SqlProof.from_schema_file("./schema.sql") as proof:
    proof.invariant(...)
```
