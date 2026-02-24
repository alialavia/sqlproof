# SqlProof — Project Specification

## Overview

SqlProof is a TypeScript library for property-based testing of SQL queries against PostgreSQL databases. It automatically generates valid test data that respects schema constraints (foreign keys, NOT NULL, CHECK, UNIQUE, enums) and runs developer-defined properties against the generated data to find counterexamples.

**Core thesis:** Developers define *properties* (universal invariants) about their SQL queries, and SqlProof generates random valid datasets to try to falsify them. When a property fails, SqlProof reports the minimal counterexample.

**Built on:** fast-check (property-based testing engine), pg (PostgreSQL client), testcontainers (disposable Postgres instances).

## Package Info

- **Name:** `sqlproof`
- **Language:** TypeScript
- **Package manager:** npm
- **Target:** Node.js 18+
- **License:** MIT
- **Database:** PostgreSQL only (v13+)

## Architecture

```
sqlproof/
├── src/
│   ├── index.ts                  # Public API exports
│   ├── schema/
│   │   ├── parser.ts             # Reads Postgres schema → internal representation
│   │   ├── types.ts              # Schema type definitions
│   │   └── dependency-graph.ts   # FK dependency ordering (topological sort)
│   ├── generators/
│   │   ├── table-generator.ts    # Generates rows for a single table
│   │   ├── dataset-generator.ts  # Generates a full multi-table dataset
│   │   ├── column-generators.ts  # Maps Postgres types → fast-check arbitraries
│   │   └── constraint-handler.ts # Handles CHECK, UNIQUE, NOT NULL constraints
│   ├── runner/
│   │   ├── property-runner.ts    # Orchestrates: generate → insert → query → check
│   │   └── db-manager.ts         # Testcontainers lifecycle, connection pooling
│   └── reporter/
│       └── reporter.ts           # Formats counterexamples for human reading
├── tests/
│   ├── schema/
│   │   └── parser.test.ts
│   ├── generators/
│   │   ├── column-generators.test.ts
│   │   └── dataset-generator.test.ts
│   └── integration/
│       └── e2e.test.ts           # Full end-to-end test with real Postgres
├── examples/
│   └── orders/
│       ├── schema.sql            # Example e-commerce schema
│       └── orders.test.ts        # Example property tests
├── package.json
├── tsconfig.json
└── README.md
```

## Core API

### Primary Interface

```typescript
import { sqlproof } from 'sqlproof';

// Define and run a property test
await sqlproof.check({
  // Human-readable name for the property
  name: "order totals match sum of line items",

  // Path to SQL schema file OR a Postgres connection string
  schema: "./schema.sql",
  // OR: schema: "postgresql://localhost:5432/mydb"

  // Number of random datasets to generate and test (default: 100)
  runs: 100,

  // Number of rows to generate per table (default: 10)
  rowsPerTable: 10,

  // The property to check. Receives a connected db client.
  // Must return true (property holds) or false (property violated).
  property: async (db) => {
    const result = await db.query(`
      SELECT o.id, o.total, SUM(li.price * li.quantity) as computed_total
      FROM orders o
      JOIN line_items li ON o.id = li.order_id
      GROUP BY o.id, o.total
    `);
    return result.rows.every(
      row => Number(row.total) === Number(row.computed_total)
    );
  }
});
```

### Configuration Options

```typescript
interface SqlProofCheckOptions {
  name: string;
  schema: string;                    // File path or connection string
  property: (db: SqlProofClient) => Promise<boolean>;
  runs?: number;                     // Default: 100
  rowsPerTable?: number;             // Default: 10
  seed?: number;                     // For reproducible failures
  timeout?: number;                  // Per-run timeout in ms (default: 5000)
  tables?: string[];                 // Subset of tables to generate data for (default: all)
  overrides?: GeneratorOverrides;    // Custom generators for specific columns
}

interface GeneratorOverrides {
  [tableName: string]: {
    [columnName: string]: fc.Arbitrary<any>;  // fast-check arbitrary
  };
}
```

### Custom Column Overrides

```typescript
import { sqlproof } from 'sqlproof';
import fc from 'fast-check';

await sqlproof.check({
  name: "discount never exceeds 50%",
  schema: "./schema.sql",
  runs: 100,
  overrides: {
    products: {
      // Override default generator for specific columns
      price: fc.float({ min: 0.01, max: 10000, noNaN: true }),
      name: fc.stringOf(fc.char(), { minLength: 1, maxLength: 100 }),
    },
    discounts: {
      percentage: fc.float({ min: 0, max: 1, noNaN: true }),
    }
  },
  property: async (db) => {
    const result = await db.query(`
      SELECT d.percentage FROM discounts d
    `);
    return result.rows.every(row => Number(row.percentage) <= 0.5);
  }
});
```

### SqlProof Client (passed to property function)

```typescript
interface SqlProofClient {
  // Run a SQL query against the test database
  query(sql: string, params?: any[]): Promise<{ rows: any[] }>;

  // Get the raw dataset that was generated (for debugging)
  getGeneratedData(): Record<string, any[]>;
}
```

### Integration with Test Runners

SqlProof should work with Jest and Vitest. The `check` function throws on property failure with a descriptive error including the counterexample.

```typescript
// Jest / Vitest
import { describe, it } from 'vitest';
import { sqlproof } from 'sqlproof';

describe('order queries', () => {
  it('totals are consistent', async () => {
    await sqlproof.check({
      name: "order totals match line items",
      schema: "./schema.sql",
      property: async (db) => {
        // ... property logic
        return true;
      }
    });
  });
});
```

## Schema Parser

### Input Formats

1. **SQL file:** Parse CREATE TABLE statements from a `.sql` file
2. **Connection string:** Connect to a live Postgres database and introspect via `information_schema` and `pg_catalog`

### What to Extract

For each table:
- Table name
- Columns: name, data type, nullable, default value
- Primary key columns
- Foreign keys: source column → target table.column
- CHECK constraints (parse simple expressions like `price > 0`, `status IN ('active', 'inactive')`)
- UNIQUE constraints
- Enum types (both native Postgres enums and CHECK-based enums)

### Internal Schema Representation

```typescript
interface SchemaInfo {
  tables: TableInfo[];
  enums: EnumInfo[];
}

interface TableInfo {
  name: string;
  columns: ColumnInfo[];
  primaryKey: string[];
  foreignKeys: ForeignKeyInfo[];
  uniqueConstraints: string[][];   // Each inner array is a set of columns
  checkConstraints: CheckConstraint[];
}

interface ColumnInfo {
  name: string;
  dataType: PostgresType;
  nullable: boolean;
  defaultValue?: string;
  isGenerated: boolean;            // SERIAL, GENERATED ALWAYS, etc.
}

interface ForeignKeyInfo {
  columns: string[];
  referencedTable: string;
  referencedColumns: string[];
}

interface CheckConstraint {
  expression: string;               // Raw SQL expression
  parsed?: ParsedCheck;             // Attempt to parse simple constraints
}

interface ParsedCheck {
  column: string;
  operator: '>' | '>=' | '<' | '<=' | '=' | 'IN' | 'BETWEEN';
  value: any;
}

interface EnumInfo {
  name: string;
  values: string[];
}
```

## Dependency Graph

### FK Ordering

Tables must be inserted in topological order based on foreign key dependencies. If `line_items.order_id` references `orders.id`, then `orders` must be populated before `line_items`.

Use Kahn's algorithm for topological sort. Detect and report circular dependencies as errors.

```typescript
function getInsertionOrder(tables: TableInfo[]): string[] {
  // Returns table names in valid insertion order
  // Throws if circular dependency detected
}
```

## Data Generators

### Column Type Mapping

Map PostgreSQL types to fast-check arbitraries:

| PostgreSQL Type | fast-check Arbitrary |
|---|---|
| `integer`, `int4` | `fc.integer({ min: -2147483648, max: 2147483647 })` |
| `bigint`, `int8` | `fc.bigInt()` |
| `smallint`, `int2` | `fc.integer({ min: -32768, max: 32767 })` |
| `serial`, `bigserial` | Skip (auto-generated) |
| `numeric(p,s)`, `decimal` | `fc.float()` with appropriate precision |
| `real`, `float4` | `fc.float({ noNaN: true, noDefaultInfinity: true })` |
| `double precision` | `fc.double({ noNaN: true, noDefaultInfinity: true })` |
| `boolean` | `fc.boolean()` |
| `text`, `varchar` | `fc.string({ minLength: 0, maxLength: 255 })` |
| `varchar(n)` | `fc.string({ maxLength: n })` |
| `char(n)` | `fc.string({ minLength: n, maxLength: n })` |
| `uuid` | `fc.uuid()` |
| `timestamp`, `timestamptz` | `fc.date()` then format |
| `date` | `fc.date()` then format date only |
| `time` | Generate valid time strings |
| `json`, `jsonb` | `fc.jsonValue()` |
| `integer[]`, etc. | `fc.array()` of base type |
| `enum types` | `fc.constantFrom(...enumValues)` |

### Constraint-Aware Generation

**NOT NULL:** Default generators already produce non-null values. For nullable columns, wrap with `fc.option()` to occasionally produce nulls.

**CHECK constraints:** Parse simple CHECK expressions and constrain the generator:
- `CHECK (price > 0)` → `fc.float({ min: 0.01, ... })`
- `CHECK (status IN ('active', 'inactive'))` → `fc.constantFrom('active', 'inactive')`
- `CHECK (quantity BETWEEN 1 AND 100)` → `fc.integer({ min: 1, max: 100 })`
- Complex CHECK constraints that can't be parsed: fall back to generate-and-filter approach

**UNIQUE constraints:** Generate values, then deduplicate. For single-column unique, use a Set to track generated values and regenerate on collision.

**Foreign keys:** Generate parent table rows first (topological order). For FK columns, use `fc.constantFrom(...parentIds)` where `parentIds` are the primary key values from already-generated parent rows.

### Dataset Generation Strategy

Use `fc.tuple()` to compose table generators in dependency order:

```typescript
// Pseudocode for dataset generation
function generateDataset(schema: SchemaInfo, rowsPerTable: number): fc.Arbitrary<Dataset> {
  const orderedTables = getInsertionOrder(schema.tables);

  // Build generators incrementally — each table's generator
  // can reference previously generated tables for FK values
  return fc.gen().map(gen => {
    const dataset: Dataset = {};
    for (const tableName of orderedTables) {
      const table = schema.tables.find(t => t.name === tableName);
      const parentData = dataset; // Already-generated parent tables
      dataset[tableName] = generateTableRows(gen, table, parentData, rowsPerTable);
    }
    return dataset;
  });
}
```

## Property Runner

### Execution Flow

For each run (up to `runs` count):

1. **Generate** a random dataset using fast-check
2. **Create** a fresh schema in the test database (use `CREATE SCHEMA` with a unique name per run for isolation, then `DROP SCHEMA CASCADE` after)
3. **Insert** generated data in FK-dependency order
4. **Execute** the user's property function, passing a connected client scoped to the test schema
5. **Check** the return value:
   - `true` → property holds, continue to next run
   - `false` → property violated, record counterexample
   - Exception thrown → treat as property violation
6. **Cleanup** the test schema

If a violation is found, fast-check handles shrinking (trying smaller/simpler values) and reports the minimal counterexample.

### Database Management

Use `testcontainers` to spin up a disposable PostgreSQL container:

```typescript
import { PostgreSqlContainer } from '@testcontainers/postgresql';

class DBManager {
  private container: StartedPostgreSqlContainer;

  async start(): Promise<void> {
    this.container = await new PostgreSqlContainer('postgres:16')
      .withReuse()  // Reuse container across runs for speed
      .start();
  }

  async createIsolatedSchema(schemaName: string): Promise<Client> {
    // Create a new schema for test isolation
    // Set search_path to the new schema
    // Return a connected client
  }

  async cleanup(schemaName: string): Promise<void> {
    // DROP SCHEMA schemaName CASCADE
  }

  async stop(): Promise<void> {
    await this.container.stop();
  }
}
```

### Schema Isolation Strategy

Instead of creating/dropping entire databases per run (slow), use PostgreSQL schemas:
- Before each run: `CREATE SCHEMA run_<uuid>`
- Set `search_path` to the new schema
- Create all tables within that schema
- After each run: `DROP SCHEMA run_<uuid> CASCADE`

This is fast and provides full isolation between runs.

## Reporter

### Counterexample Output

When a property fails, output:

```
✗ Property failed: "order totals match sum of line items"

  After 23 runs (seed: 1708891234)

  Counterexample (shrunk 3 times):

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

  Expected: total (100.00) === sum(price * quantity) (110.00)

  Reproduce with seed: sqlproof.check({ ..., seed: 1708891234 })
```

## Example: E-Commerce Schema

### schema.sql

```sql
CREATE TYPE order_status AS ENUM ('pending', 'confirmed', 'shipped', 'delivered', 'cancelled');

CREATE TABLE customers (
  id SERIAL PRIMARY KEY,
  name VARCHAR(100) NOT NULL,
  email VARCHAR(255) NOT NULL UNIQUE,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  status order_status NOT NULL DEFAULT 'pending',
  total NUMERIC(10,2) NOT NULL CHECK (total >= 0),
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE products (
  id SERIAL PRIMARY KEY,
  name VARCHAR(200) NOT NULL,
  price NUMERIC(10,2) NOT NULL CHECK (price > 0),
  stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0)
);

CREATE TABLE line_items (
  id SERIAL PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  price NUMERIC(10,2) NOT NULL CHECK (price > 0)
);
```

### Example Property Tests

```typescript
import { describe, it } from 'vitest';
import { sqlproof } from 'sqlproof';

describe('e-commerce properties', () => {
  const schema = './examples/orders/schema.sql';

  it('order total should be non-negative', async () => {
    await sqlproof.check({
      name: "order totals are non-negative",
      schema,
      property: async (db) => {
        const result = await db.query('SELECT total FROM orders');
        return result.rows.every(row => Number(row.total) >= 0);
      }
    });
  });

  it('every line item references a valid order', async () => {
    await sqlproof.check({
      name: "line items have valid order references",
      schema,
      property: async (db) => {
        const result = await db.query(`
          SELECT li.id
          FROM line_items li
          LEFT JOIN orders o ON li.order_id = o.id
          WHERE o.id IS NULL
        `);
        return result.rows.length === 0;
      }
    });
  });

  it('order total equals sum of line item costs', async () => {
    await sqlproof.check({
      name: "order totals match line items",
      schema,
      runs: 200,
      property: async (db) => {
        const result = await db.query(`
          SELECT
            o.id,
            o.total as stored_total,
            COALESCE(SUM(li.price * li.quantity), 0) as computed_total
          FROM orders o
          LEFT JOIN line_items li ON o.id = li.order_id
          GROUP BY o.id, o.total
        `);
        return result.rows.every(row =>
          Math.abs(Number(row.stored_total) - Number(row.computed_total)) < 0.01
        );
      }
    });
  });

  it('cancelled orders have no new line items after cancellation', async () => {
    await sqlproof.check({
      name: "cancelled orders are immutable",
      schema,
      property: async (db) => {
        const result = await db.query(`
          SELECT o.id, o.status, COUNT(li.id) as item_count
          FROM orders o
          LEFT JOIN line_items li ON o.id = li.order_id
          WHERE o.status = 'cancelled'
          GROUP BY o.id, o.status
        `);
        // This property will likely always pass with random data,
        // but demonstrates the pattern for business rule testing
        return true;
      }
    });
  });
});
```

## Dependencies

```json
{
  "dependencies": {
    "fast-check": "^3.x",
    "pg": "^8.x"
  },
  "devDependencies": {
    "@testcontainers/postgresql": "^10.x",
    "typescript": "^5.x",
    "vitest": "^1.x",
    "tsup": "^8.x"
  },
  "peerDependencies": {
    "@testcontainers/postgresql": "^10.x"
  }
}
```

Note: `testcontainers` is a peer dependency — users who want the auto-managed Postgres container install it themselves. Users can also pass a connection string to an existing Postgres instance.

## Build & Publish

- **Bundler:** tsup (generates ESM + CJS)
- **Testing:** vitest
- **Output:** `dist/` with type declarations

```json
{
  "main": "dist/index.cjs",
  "module": "dist/index.js",
  "types": "dist/index.d.ts",
  "exports": {
    ".": {
      "import": "./dist/index.js",
      "require": "./dist/index.cjs"
    }
  }
}
```

## What's NOT in v0.1 (Future Scope)

- **LLM-suggested properties** (v0.2): Point SqlProof at a schema + queries, LLM proposes properties to test
- **Coverage metrics** (v0.2): Track which query branches/conditions have been exercised
- **Dataset-level shrinking** (v0.2): Reduce number of rows, not just row values
- **CI/CD integration** (v0.2): GitHub Actions recipe, JUnit XML output
- **Multiple databases** (v0.3): MySQL, SQLite support
- **Workflow testing** (v0.3): Temporal/trigger.dev property testing
- **Formal verification mode** (v1.0): SMT-solver backed verification for critical properties
- **GUI/dashboard**: Visual exploration of test results
- **VS Code extension**: In-editor property definition and results

## Implementation Priority (Weekend)

### Saturday
1. Project scaffolding (package.json, tsconfig, tsup config)
2. Schema parser — SQL file parsing for CREATE TABLE, constraints, enums, FKs
3. Dependency graph — topological sort of tables by FK references
4. Column generators — map Postgres types to fast-check arbitraries
5. Constraint handler — NOT NULL, CHECK (simple expressions), UNIQUE, FK references
6. Dataset generator — compose table generators in dependency order

### Sunday
1. DB manager — Testcontainers setup, schema isolation
2. Property runner — generate → insert → query → check loop using fast-check
3. Reporter — format counterexamples as readable tables
4. Public API — clean `sqlproof.check()` interface
5. Example — e-commerce schema with 3-4 property tests
6. README — installation, quick start, API reference

