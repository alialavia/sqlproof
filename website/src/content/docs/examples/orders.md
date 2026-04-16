---
title: E-Commerce Orders
description: A complete walkthrough using SqlProof with a realistic e-commerce schema.
---

This example walks through using SqlProof with a realistic e-commerce schema that has four tables, foreign keys, CHECK constraints, and an enum type.

## The Schema

```sql
CREATE TYPE order_status AS ENUM ('pending', 'confirmed', 'shipped', 'delivered', 'cancelled');

CREATE TABLE customers (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  email VARCHAR(255) NOT NULL UNIQUE,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  name VARCHAR(200) NOT NULL,
  price NUMERIC(10,2) NOT NULL CHECK (price > 0),
  stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0)
);

CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  status order_status NOT NULL DEFAULT 'pending',
  total NUMERIC(10,2) NOT NULL CHECK (total >= 0),
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE line_items (
  id SERIAL PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  price NUMERIC(10,2) NOT NULL CHECK (price > 0)
);
```

## Test File

```typescript
import { describe, it, beforeEach, afterEach } from 'vitest';
import { SqlProof } from 'sqlproof';

const schemaFile = new URL('./schema.sql', import.meta.url).pathname;

describe('e-commerce properties', { timeout: 120_000 }, () => {
  let proof: SqlProof;

  beforeEach(async () => {
    proof = await SqlProof.connect({ schemaFile });
  }, 120_000);

  afterEach(async () => {
    await proof?.disconnect();
  });

  it('order totals are always non-negative', async () => {
    await proof.check('order totals are non-negative', {
      generate: { customers: 5, orders: 10, products: 5, line_items: 20 },
      property: async (db) => {
        const result = await db.query('SELECT total FROM orders');
        return result.rows.every(row => Number(row.total) >= 0);
      },
      runs: 50,
    });
  });

  it('every line item references a valid order', async () => {
    await proof.invariant('no orphan line items', {
      generate: { customers: 5, orders: 10, products: 5, line_items: 20 },
      query: `
        SELECT li.id FROM line_items li
        LEFT JOIN orders o ON li.order_id = o.id
        WHERE o.id IS NULL
      `,
      expectEmpty: true,
      runs: 50,
    });
  });

  it('order total equals sum of line item costs (demonstrates a failing property)', async () => {
    try {
      await proof.check('order totals match line items', {
        generate: { customers: 5, orders: 5, products: 5, line_items: 10 },
        property: async (db) => {
          const result = await db.query(`
            SELECT
              o.total as stored_total,
              COALESCE(SUM(li.price * li.quantity), 0) as computed_total
            FROM orders o
            LEFT JOIN line_items li ON o.id = li.order_id
            GROUP BY o.id, o.total
          `);
          return result.rows.every(
            row => Math.abs(Number(row.stored_total) - Number(row.computed_total)) < 0.01,
          );
        },
        runs: 50,
      });
    } catch (err) {
      console.log((err as Error).message);
    }
  });

  it('FK integrity holds under zipf distribution', async () => {
    proof
      .customize('orders', { fkDistribution: { customer_id: 'zipf' } })
      .customize('line_items', {
        fkDistribution: { order_id: 'zipf', product_id: 'adversarial' },
      });

    await proof.invariant('FK integrity with skewed distribution', {
      generate: { customers: 5, orders: 20, products: 5, line_items: 50 },
      query: `
        SELECT li.id FROM line_items li
        LEFT JOIN orders o ON li.order_id = o.id
        WHERE o.id IS NULL
      `,
      expectEmpty: true,
      runs: 20,
    });
  });
});
```

## What to Expect

The first three tests pass — SqlProof generates data respecting schema constraints. The "order total equals sum of line items" test **intentionally fails** to demonstrate counterexample output:

```
✗ Property failed: "order totals match line items"

  After 1 run(s) (seed: 1708891234)

  Counterexample (shrunk 4 time(s)):

  Table: orders
  ┌────┬────────┐
  │ id │ total  │
  ├────┼────────┤
  │ 1  │ 100.00 │
  └────┴────────┘

  Table: line_items
  ┌────┬──────────┬───────┬──────────┐
  │ id │ order_id │ price │ quantity │
  ├────┼──────────┼───────┼──────────┤
  │ 1  │ 1        │ 30.00 │ 2        │
  └────┴──────────┴───────┴──────────┘

  Reproduce: proof.check('...', { ..., seed: 1708891234 })
```

The source code lives in `examples/orders/` in the repository.
