---
title: E-Commerce Orders
description: A complete Python walkthrough using SqlProof with an e-commerce schema.
---

This example tests simple invariants against an e-commerce schema with customers,
orders, products, line items, foreign keys, CHECK constraints, and an enum.

## Schema

```sql
CREATE TABLE customers (
  id SERIAL PRIMARY KEY,
  email VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  total NUMERIC(10,2) NOT NULL CHECK (total >= 0)
);
```

## Test File

```python
from decimal import Decimal
from sqlproof import SqlProof, sqlproof

proof = SqlProof.from_schema_file("./schema.sql")


@sqlproof(proof, sizes={"customers": 5, "orders": 10}, runs=50)
def test_order_totals_non_negative(db):
    rows = db.query("SELECT total FROM orders")
    assert all(row["total"] >= 0 for row in rows)


@sqlproof(proof, sizes={"customers": 5, "orders": 10}, runs=50)
def test_no_orphan_orders(db):
    rows = db.query("""
        SELECT o.id
        FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        WHERE c.id IS NULL
    """)
    assert rows == []
```

## Failure Output

When a property fails, SqlProof writes a minimal counterexample JSON with the
generated dataset, row context, seed, schema fingerprint, and reproduction details.

```bash
pytest examples/ecommerce/test_orders.py --sqlproof-seed=1708891234
```

The bundled example lives in `examples/ecommerce/`.
