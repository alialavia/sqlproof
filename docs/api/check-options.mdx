---
title: Check Options
description: Options for proof.check() and the @sqlproof decorator.
---

`proof.check()` and `@sqlproof` share the same core ideas: table sizes, run count,
optional seed, optional setup, and a property function.

## Decorator Form

```python
@sqlproof(
    proof,
    sizes={"customers": 20, "orders": 100, "line_items": 500},
    runs=100,
    seed=1708891234,
    timeout_ms=5000,
)
def test_order_totals_non_negative(db):
    rows = db.query("SELECT total FROM orders")
    assert all(row["total"] >= 0 for row in rows)
```

## Imperative Form

```python
def check_totals(db):
    rows = db.query("SELECT total FROM orders")
    assert all(row["total"] >= 0 for row in rows)


proof.check(
    name="order totals are non-negative",
    sizes={"customers": 20, "orders": 100},
    property=check_totals,
    runs=100,
)
```

## Fields

| Field        | Description                                      |
| ------------ | ------------------------------------------------ |
| `sizes`      | Per-table row counts for generated datasets      |
| `property`   | Callable that asserts the SQL property           |
| `setup`      | Optional callable run after data insertion       |
| `runs`       | Number of generated datasets to test             |
| `seed`       | Reproduce a specific generation and shrink trace |
| `timeout_ms` | Per-run timeout in milliseconds                  |
| `commit`     | Use schema-isolation mode instead of rollback    |

## SqlProofClient

The `db` object exposes convenience helpers:

```python
rows = db.query("SELECT id, total FROM orders")
total = db.scalar("SELECT SUM(total) FROM orders")
affected = db.execute("UPDATE orders SET status = %s", "confirmed")
dataset = db.get_generated_data()
```

Use `db.connection` when you need raw psycopg access.
