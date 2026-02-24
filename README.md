# SqlProof

Property-based testing for SQL queries against PostgreSQL. Define invariants about your queries, and SqlProof generates random valid datasets to try to break them — then reports the minimal counterexample when it does.

Built on [fast-check](https://github.com/dubzzz/fast-check), [pg](https://node-postgres.com/), and [testcontainers](https://node.testcontainers.org/).

## Install

```bash
npm install sqlproof
```

For automatic disposable Postgres instances (no external DB needed):

```bash
npm install -D @testcontainers/postgresql
```

Requires Node.js 18+ and PostgreSQL 13+. If using testcontainers, Docker must be running.

## Quick Start

Given a schema file:

```sql
-- schema.sql
CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  customer_id INTEGER NOT NULL,
  total NUMERIC(10,2) NOT NULL CHECK (total >= 0)
);

CREATE TABLE line_items (
  id SERIAL PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  price NUMERIC(10,2) NOT NULL CHECK (price > 0)
);
```

Write property tests with Vitest (or Jest):

```typescript
import { describe, it } from 'vitest';
import { sqlproof } from 'sqlproof';

describe('order queries', () => {
  it('every line item references a valid order', async () => {
    await sqlproof.check({
      name: 'line items have valid order references',
      schema: './schema.sql',
      runs: 50,
      rowsPerTable: 5,
      property: async (db) => {
        const result = await db.query(`
          SELECT li.id
          FROM line_items li
          LEFT JOIN orders o ON li.order_id = o.id
          WHERE o.id IS NULL
        `);
        return result.rows.length === 0;
      },
    });
  });
});
```

SqlProof will:

1. Parse your schema (tables, columns, FKs, CHECK constraints, enums)
2. Spin up a disposable Postgres container via testcontainers
3. For each run, generate a random dataset that respects schema constraints
4. Insert it into an isolated Postgres schema
5. Run your property function against the data
6. If the property returns `false` (or throws), shrink the dataset to find the minimal counterexample

## API

### `sqlproof.check(options)`

The main entry point. Returns a `Promise<void>` that resolves on success and throws a `SqlProofError` with a formatted counterexample on failure.

```typescript
await sqlproof.check({
  name: 'order totals are non-negative',
  schema: './schema.sql',
  property: async (db) => {
    const result = await db.query('SELECT total FROM orders');
    return result.rows.every(row => Number(row.total) >= 0);
  },
});
```

### Options

| Option | Type | Default | Description |
|---|---|---|---|
| `name` | `string` | *required* | Human-readable property name |
| `schema` | `string` | *required* | Path to a `.sql` file or a `postgresql://` connection string |
| `property` | `(db) => Promise<boolean>` | *required* | The invariant to check. Return `true` if it holds. |
| `runs` | `number` | `100` | Number of random datasets to generate |
| `rowsPerTable` | `number` | `10` | Rows to generate per table |
| `seed` | `number` | — | Reproduce a specific failure |
| `timeout` | `number` | `5000` | Per-run timeout in ms |
| `tables` | `string[]` | all | Subset of tables to generate data for |
| `overrides` | `GeneratorOverrides` | — | Custom fast-check arbitraries for specific columns |

### Schema Sources

**SQL file** — SqlProof parses `CREATE TABLE`, `CREATE TYPE ... AS ENUM`, foreign keys, CHECK constraints, UNIQUE constraints, and column types directly from `.sql` files.

**Connection string** — Pass a `postgresql://` URL and SqlProof introspects the live database via `information_schema` and `pg_catalog`.

```typescript
await sqlproof.check({
  schema: 'postgresql://localhost:5432/mydb',
  // ...
});
```

### Custom Column Generators

Override default data generation for specific columns using fast-check arbitraries:

```typescript
import fc from 'fast-check';

await sqlproof.check({
  name: 'discount never exceeds 50%',
  schema: './schema.sql',
  overrides: {
    products: {
      price: fc.float({ min: 0.01, max: 10000, noNaN: true }),
    },
    discounts: {
      percentage: fc.float({ min: 0, max: 1, noNaN: true }),
    },
  },
  property: async (db) => {
    const result = await db.query('SELECT percentage FROM discounts');
    return result.rows.every(row => Number(row.percentage) <= 0.5);
  },
});
```

### The `db` Client

The property function receives a `SqlProofClient`:

```typescript
interface SqlProofClient {
  query(sql: string, params?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
  getGeneratedData(): Record<string, Record<string, unknown>[]>;
}
```

- `query()` runs SQL against the isolated test schema for the current run
- `getGeneratedData()` returns the inserted dataset (useful for debugging)

## Failure Output

When a property fails, SqlProof throws with a formatted counterexample:

```
✗ Property failed: "order totals match sum of line items"

  After 23 run(s) (seed: 1708891234)

  Counterexample (shrunk 3 time(s)):

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
  │ 2  │ 1        │ 50.00 │ 1        │
  └────┴──────────┴───────┴──────────┘

  Reproduce: sqlproof.check({ ..., seed: 1708891234 })
```

Use the reported `seed` to reproduce the exact failure deterministically.

## How It Works

1. **Schema parsing** — Reads your SQL file (or introspects a live DB) to extract tables, columns, types, foreign keys, CHECK/UNIQUE constraints, and enums

2. **Dependency ordering** — Topologically sorts tables by foreign key references so parent rows are always inserted first

3. **Data generation** — Maps PostgreSQL types to fast-check arbitraries (`integer` to `fc.integer()`, `varchar(100)` to `fc.string({ maxLength: 100 })`, enum types to `fc.constantFrom(...)`, etc.) and applies constraint-aware generation for CHECK, UNIQUE, NOT NULL, and FK constraints

4. **Isolated execution** — Each run creates a fresh `CREATE SCHEMA run_<id>`, inserts the generated data, runs your property, then `DROP SCHEMA ... CASCADE`. This is fast and provides full isolation without creating/dropping entire databases.

5. **Shrinking** — When a property fails, fast-check automatically shrinks the dataset to find the simplest counterexample

## Supported PostgreSQL Types

`integer`, `smallint`, `bigint`, `serial`, `bigserial`, `numeric(p,s)`, `real`, `double precision`, `boolean`, `text`, `varchar(n)`, `char(n)`, `uuid`, `timestamp`, `timestamptz`, `date`, `time`, `json`, `jsonb`, `bytea`, array types, and custom `ENUM` types.

## Development

```bash
git clone https://github.com/your-org/sqlproof.git
cd sqlproof
npm install

npm test                  # unit tests (no Docker needed)
npm run test:integration  # e2e tests (requires Docker)
npm run build             # build with tsup
npm run typecheck         # type-check with tsc
```

## License

MIT
