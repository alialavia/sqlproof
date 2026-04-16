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
import { SqlProof } from 'sqlproof';

// Connect once per test suite
const proof = await SqlProof.connect({ schemaFile: './schema.sql' });
// OR connect to an existing Postgres instance:
// const proof = await SqlProof.connect({ connectionString: 'postgresql://localhost:5432/mydb' });

// Register custom generators or FK distribution strategies
proof.customize('products', {
  price: fc.float({ min: 0.01, max: 9999.99, noNaN: true }),
  name: fc.string({ minLength: 1, maxLength: 100 }),
});

proof.customize('orders', {
  fkDistribution: { customer_id: 'zipf' },
});

// Run a property test
await proof.check('order totals are non-negative', {
  generate: { customers: 20, orders: 100, line_items: 500 },
  runs: 100,
  property: async (db) => {
    const result = await db.query('SELECT total FROM orders');
    return result.rows.every(row => Number(row.total) >= 0);
  },
});

// Declarative shorthand: asserts query returns 0 rows
await proof.invariant('no orphan line items', {
  generate: { customers: 10, orders: 20, products: 10, line_items: 50 },
  query: `
    SELECT li.id FROM line_items li
    LEFT JOIN orders o ON li.order_id = o.id
    WHERE o.id IS NULL
  `,
  expectEmpty: true,
});

// Close connection and stop testcontainers (if auto-managed)
await proof.disconnect();
```

### Connect Options

```typescript
interface SqlProofConnectOptions {
  connectionString?: string;  // Connect to an existing Postgres instance
  schema?: string;            // Schema name to introspect (default: 'public')
  schemaFile?: string;        // Path to SQL DDL file — auto-starts testcontainers
}
```

Exactly one of `connectionString` or `schemaFile` must be provided.

### SqlProof Class

```typescript
class SqlProof {
  /** Factory: connect to Postgres, introspect schema, return ready instance. */
  static async connect(options: SqlProofConnectOptions): Promise<SqlProof>

  /** Register custom generators or FK distribution strategies for a table. Fluent. */
  customize(table: string, overrides: TableCustomization): this

  /** Run a property-based test. Throws SqlProofError on failure with counterexample. */
  async check(name: string, options: CheckOptions): Promise<void>

  /** Declarative shorthand: asserts the query returns 0 rows for all generated datasets. */
  async invariant(name: string, options: InvariantOptions): Promise<void>

  /** Close DB connection and stop testcontainers instance (if auto-managed). */
  async disconnect(): Promise<void>
}
```

### Check Options

```typescript
interface CheckOptions {
  /** Per-table row counts, e.g. { customers: 20, orders: 100, line_items: 500 } */
  generate: Record<string, number>;
  /** Optional mutations to run after data insertion, before the property. */
  setup?: (db: SqlProofClient) => Promise<void>;
  /** Returns true if the property holds, false if violated. */
  property: (db: SqlProofClient) => Promise<boolean>;
  /** Number of random datasets to generate and test. Default: 100. */
  runs?: number;
  /** Seed for reproducible failures. */
  seed?: number;
  /** Per-run timeout in ms. Default: 5000. */
  timeout?: number;
}
```

### Invariant Options

```typescript
interface InvariantOptions {
  generate: Record<string, number>;
  /** SQL query that must return 0 rows for the invariant to hold. */
  query: string;
  expectEmpty: true;
  runs?: number;
  seed?: number;
  timeout?: number;
}
```

### Table Customization

```typescript
interface TableCustomization {
  /** FK distribution strategy per FK column name. */
  fkDistribution?: Record<string, FkDistributionStrategy>;
  /** Custom fast-check arbitrary per column name. */
  [columnName: string]: fc.Arbitrary<unknown> | Record<string, FkDistributionStrategy> | undefined;
}

type FkDistributionStrategy = 'zipf' | 'uniform' | 'adversarial';
```

| Strategy | Behavior | Use case |
|---|---|---|
| `uniform` (default) | Equal probability per parent | Good general coverage |
| `zipf` | First parents get many children, later ones few or none | Realistic skewed load |
| `adversarial` | Only picks first, middle, last parent | Boundary stress test |

### SqlProof Client (passed to property function)

```typescript
interface SqlProofClient {
  query(sql: string, params?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
  getGeneratedData(): Dataset;
}
```

### Integration with Test Runners

SqlProof works with Jest and Vitest. `check()` throws on property failure with a descriptive error including the counterexample.

```typescript
import { describe, it, beforeEach, afterEach } from 'vitest';
import { SqlProof } from 'sqlproof';

describe('order queries', () => {
  let proof: SqlProof;

  beforeEach(async () => {
    proof = await SqlProof.connect({ schemaFile: './schema.sql' });
  }, 120000);

  afterEach(async () => {
    await proof?.disconnect();
  });

  it('totals are consistent', async () => {
    await proof.check('order totals match line items', {
      generate: { customers: 5, orders: 10, line_items: 20 },
      property: async (db) => {
        // ... property logic
        return true;
      },
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

  Reproduce with seed: proof.check('...', { ..., seed: 1708891234 })
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
import { describe, it, beforeEach, afterEach } from 'vitest';
import { SqlProof } from 'sqlproof';

describe('e-commerce properties', { timeout: 120000 }, () => {
  let proof: SqlProof;

  beforeEach(async () => {
    proof = await SqlProof.connect({ schemaFile: './examples/orders/schema.sql' });
  }, 120000);

  afterEach(async () => {
    await proof?.disconnect();
  });

  it('order total should be non-negative', async () => {
    await proof.check('order totals are non-negative', {
      generate: { customers: 5, orders: 5, products: 5, line_items: 10 },
      property: async (db) => {
        const result = await db.query('SELECT total FROM orders');
        return result.rows.every(row => Number(row.total) >= 0);
      },
    });
  });

  it('every line item references a valid order', async () => {
    await proof.check('line items have valid order references', {
      generate: { customers: 5, orders: 5, products: 5, line_items: 10 },
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

  it('order total equals sum of line item costs', async () => {
    await proof.check('order totals match line items', {
      generate: { customers: 5, orders: 5, products: 5, line_items: 10 },
      runs: 50,
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
          Math.abs(Number(row.stored_total) - Number(row.computed_total)) < 0.01,
        );
      },
    });
  });

  it('no orphan line items', async () => {
    await proof.invariant('line items always have a valid order', {
      generate: { customers: 5, orders: 10, products: 5, line_items: 20 },
      query: `
        SELECT li.id FROM line_items li
        LEFT JOIN orders o ON li.order_id = o.id
        WHERE o.id IS NULL
      `,
      expectEmpty: true,
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

