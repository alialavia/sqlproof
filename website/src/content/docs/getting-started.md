---
title: Getting Started
description: Install SqlProof and write your first property test in minutes.
---

SqlProof is a property-based testing library for PostgreSQL. It generates random valid datasets that respect your schema constraints, runs your properties against them, and reports the minimal counterexample when one fails.

## Prerequisites

- Node.js 18+
- PostgreSQL 13+ (or Docker, if using testcontainers)

## Install

```bash
npm install sqlproof
```

For automatic disposable Postgres instances (no external DB required):

```bash
npm install -D @testcontainers/postgresql
```

If using testcontainers, Docker must be running.

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

Write property tests with Vitest (or Jest):

```typescript
import { describe, it, beforeEach, afterEach } from 'vitest';
import { SqlProof } from 'sqlproof';

describe('order queries', () => {
  let proof: SqlProof;

  beforeEach(async () => {
    proof = await SqlProof.connect({ schemaFile: './schema.sql' });
  }, 120_000);

  afterEach(async () => {
    await proof?.disconnect();
  });

  it('every order has a valid customer', async () => {
    await proof.invariant('no orphan orders', {
      generate: { customers: 10, orders: 50 },
      query: `
        SELECT o.id FROM orders o
        LEFT JOIN customers c ON o.customer_id = c.id
        WHERE c.id IS NULL
      `,
      expectEmpty: true,
      runs: 50,
    });
  });
});
```

## Connection Modes

SqlProof supports three connection modes:

| Mode | Options | Docker needed? | When to use |
|------|---------|----------------|-------------|
| Testcontainers | `schemaFile` only | Yes | Local dev with no external DB |
| Connection string | `connectionString` + optionally `schemaFile` or `schema` | No | CI Postgres, staging, Supabase, Render |
| Neon branching | `neon: { apiKey, projectId }` | No | Instant isolated branches on Neon |

See the [Local Development](/guides/local-dev), [CI/CD Integration](/guides/ci-cd), and [Security & Credentials](/guides/security) guides for full setup instructions.

### Connection String

Point SqlProof at any running Postgres. Provide a DDL file to apply your schema, or introspect an existing one:

```typescript
// Apply DDL to the external DB — no Docker needed
const proof = await SqlProof.connect({
  connectionString: process.env.DATABASE_URL!,
  schemaFile: './schema.sql',
});

// Introspect an existing live schema
const proof = await SqlProof.connect({
  connectionString: process.env.DATABASE_URL!,
  schema: 'public',
});
```

### Neon Branching

Creates an instant isolated branch for each test session (~1 second). Deleted automatically on `disconnect()`.

```typescript
const proof = await SqlProof.connect({
  neon: {
    apiKey: process.env.NEON_API_KEY!,
    projectId: process.env.NEON_PROJECT_ID!,
    parentBranch: 'main', // optional
  },
  schema: 'public',
});
```

## Vitest Configuration

Add `pool: 'forks'` to your Vitest config — required for testcontainers compatibility:

```typescript
// vitest.config.ts
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    pool: 'forks',
  },
});
```

## What Happens Under the Hood

1. **Schema parsing** — reads your `.sql` file (or introspects the DB) to extract tables, columns, FKs, CHECK/UNIQUE constraints, and enum types
2. **Topological sort** — orders tables by FK dependencies so parent rows are always inserted before children
3. **Data generation** — maps Postgres types to [fast-check](https://github.com/dubzzz/fast-check) arbitraries and applies constraint-aware generation
4. **Schema isolation** — each run creates `CREATE SCHEMA run_<uuid>`, inserts data, runs your property, then `DROP SCHEMA CASCADE`
5. **Shrinking** — when a property fails, fast-check shrinks the dataset to the smallest counterexample and reports it with a reproducible seed
