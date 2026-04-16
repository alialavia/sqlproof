---
title: SqlProof Class
description: The main class for connecting to Postgres and running property tests.
---

The `SqlProof` class is the entry point for all property tests. Create one instance per test suite via `SqlProof.connect()`, share it across all `check()` and `invariant()` calls, then call `disconnect()` in cleanup.

## `SqlProof.connect(options)`

Factory method. Connects to Postgres (or starts a testcontainers instance), introspects the schema, and returns a ready `SqlProof` instance.

```typescript
static async connect(options: SqlProofConnectOptions): Promise<SqlProof>
```

**Options:**

| Field | Type | Description |
|---|---|---|
| `schemaFile` | `string` | Path to a `.sql` DDL file. Auto-starts a testcontainers Postgres. |
| `connectionString` | `string` | `postgresql://` URL. Connects to an existing Postgres instance. |
| `schema` | `string` | Postgres schema name to introspect. Default: `'public'`. Only used with `connectionString`. |

Exactly one of `schemaFile` or `connectionString` must be provided.

**Example:**

```typescript
// With a SQL file (auto-manages Postgres via testcontainers):
const proof = await SqlProof.connect({ schemaFile: './schema.sql' });

// With an existing database:
const proof = await SqlProof.connect({
  connectionString: 'postgresql://localhost:5432/mydb',
});
```

---

## `proof.customize(table, overrides)`

Register custom generators or FK distribution strategies for a table. Returns `this` for fluent chaining. Must be called before `check()` or `invariant()`.

```typescript
customize(table: string, overrides: TableCustomization): this
```

**Example:**

```typescript
import fc from 'fast-check';

proof
  .customize('products', {
    price: fc.float({ min: 0.01, max: 9999.99, noNaN: true }),
    name: fc.string({ minLength: 1, maxLength: 100 }),
  })
  .customize('orders', {
    fkDistribution: { customer_id: 'zipf' },
  });
```

---

## `proof.check(name, options)`

Run a property-based test. Throws `SqlProofError` on failure with a formatted counterexample including a reproducible seed.

```typescript
async check(name: string, options: CheckOptions): Promise<void>
```

**Example:**

```typescript
await proof.check('order totals are non-negative', {
  generate: { customers: 10, orders: 50, line_items: 200 },
  property: async (db) => {
    const result = await db.query('SELECT total FROM orders');
    return result.rows.every(row => Number(row.total) >= 0);
  },
  runs: 100,
});
```

---

## `proof.invariant(name, options)`

Declarative shorthand: asserts that a SQL query returns zero rows for all generated datasets.

```typescript
async invariant(name: string, options: InvariantOptions): Promise<void>
```

| Field | Type | Description |
|---|---|---|
| `generate` | `Record<string, number>` | Per-table row counts. |
| `query` | `string` | SQL query. Must return 0 rows for the invariant to hold. |
| `expectEmpty` | `true` | Always `true` — makes the intent explicit. |
| `runs` | `number` | Number of datasets to test. Default: `100`. |
| `seed` | `number` | Reproduce a specific failure. |
| `timeout` | `number` | Per-run timeout in ms. Default: `5000`. |

**Example:**

```typescript
await proof.invariant('no orphan line items', {
  generate: { customers: 10, orders: 50, line_items: 200 },
  query: `
    SELECT li.id FROM line_items li
    LEFT JOIN orders o ON li.order_id = o.id
    WHERE o.id IS NULL
  `,
  expectEmpty: true,
  runs: 50,
});
```

---

## `proof.disconnect()`

Close the Postgres connection and stop the testcontainers instance (if auto-managed). Call in `afterEach` or `afterAll`.

```typescript
async disconnect(): Promise<void>
```

**Example:**

```typescript
afterEach(async () => {
  await proof?.disconnect();
});
```
