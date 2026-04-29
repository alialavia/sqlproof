---
title: Getting Started
description: Install SqlProof and write your first Python property test in minutes.
---

SqlProof is a Python property-based testing library for PostgreSQL. It generates random valid datasets that respect your schema constraints, runs your properties against them, and reports the minimal counterexample when one fails.

## Prerequisites

- Python 3.11+
- PostgreSQL 13+ or Docker, if using testcontainers

## Install

```bash
pip install sqlproof
```

## Quick Start

Given a schema file:

```sql
-- schema.sql
CREATE TABLE customers (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  email VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  total NUMERIC(10,2) NOT NULL CHECK (total >= 0)
);
```

Write a pytest property:

```python
from sqlproof import SqlProof, sqlproof

proof = SqlProof.from_schema_file("./schema.sql")


@sqlproof(proof, sizes={"customers": 10, "orders": 50}, runs=50)
def test_order_totals_non_negative(db):
    rows = db.query("SELECT total FROM orders")
    assert all(row["total"] >= 0 for row in rows)
```

Run it:

```bash
pytest
```

## Connection Modes

SqlProof supports two primary connection modes:

| Mode              | Options                        | Docker needed? | When to use                              |
| ----------------- | ------------------------------ | -------------- | ---------------------------------------- |
| Testcontainers    | `schema_file` only             | Yes            | Local development with no external DB    |
| Connection string | `connection_string` + `schema` | No             | CI Postgres, staging, Supabase, Render   |

## What Happens Under the Hood

1. **Schema parsing** reads your `.sql` file or introspects a live DB.
2. **Topological sort** orders tables by FK dependencies.
3. **Data generation** maps Postgres types to Hypothesis strategies.
4. **Run isolation** inserts generated data, runs your property, then rolls back or drops the run schema.
5. **Shrinking** uses Hypothesis to minimize failing counterexamples.
