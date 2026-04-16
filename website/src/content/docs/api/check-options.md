---
title: CheckOptions
description: Options for proof.check() — per-table row counts, property function, and test configuration.
---

`CheckOptions` is passed as the second argument to `proof.check()`.

```typescript
interface CheckOptions {
  generate: Record<string, number>;
  setup?: (db: SqlProofClient) => Promise<void>;
  property: (db: SqlProofClient) => Promise<boolean>;
  runs?: number;
  seed?: number;
  timeout?: number;
}
```

## Fields

### `generate` (required)

Per-table row counts. Keys are table names; values are the number of rows to generate per run. Only tables listed here will have data generated.

```typescript
generate: { customers: 20, orders: 100, line_items: 500 }
```

### `property` (required)

A function that receives a `SqlProofClient` and returns `Promise<boolean>`. Return `true` if the property holds, `false` if violated. Throwing also counts as a violation.

```typescript
property: async (db) => {
  const result = await db.query('SELECT total FROM orders WHERE total < 0');
  return result.rows.length === 0;
}
```

### `setup` (optional)

Runs after data insertion but before the property check. Use for mutations or additional setup.

```typescript
setup: async (db) => {
  await db.query(`UPDATE orders SET status = 'confirmed' WHERE total > 100`);
}
```

### `runs` (optional)

Number of random datasets to generate and test. Default: `100`.

### `seed` (optional)

Integer seed for deterministic data generation. Use the seed from a failure report to reproduce it:

```typescript
await proof.check('order totals are non-negative', {
  generate: { customers: 10, orders: 50 },
  property: async (db) => { /* ... */ },
  seed: 1708891234,
});
```

### `timeout` (optional)

Per-run timeout in milliseconds. Default: `5000`.

## `SqlProofClient`

The `db` object passed to `property` and `setup`:

```typescript
interface SqlProofClient {
  query(sql: string, params?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
  getGeneratedData(): Dataset;
}
```

- `query()` — runs SQL against the isolated test schema for the current run
- `getGeneratedData()` — returns the full inserted dataset (useful for debugging)
