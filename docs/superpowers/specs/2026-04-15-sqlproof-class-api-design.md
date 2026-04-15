# SqlProof Class API — Design Document

**Date:** 2026-04-15
**Status:** Approved
**Author:** Ali Alavi

---

## 1. Overview

Refactor the public API from a flat functional style (`sqlproof.check({...})`) to a
class-based style (`SqlProof.connect()` → `proof.check()`) that matches the PRD. The DB
lifecycle moves from inside the runner to the `SqlProof` class so a single Postgres
connection (or testcontainers instance) is shared across all property checks in a test
suite.

---

## 2. Architecture

The `SqlProof` class becomes the central coordinator:

```
SqlProof
  ├── DBManager          ← started once on connect(), stopped on disconnect()
  ├── SchemaInfo         ← introspected once on connect()
  ├── customizations     ← Map<tableName, TableCustomization>
  └── check() / invariant() / customize()
        └── runChecks()  ← internal, receives live DBManager + SchemaInfo
              └── fc.assert + makeDatasetArbitrary()
```

The existing `runProperty()` function in `property-runner.ts` is replaced by a simpler
internal `runChecks()` function that accepts a live DBManager and pre-parsed SchemaInfo.
No more starting containers or reading files inside the runner.

---

## 3. Public API

### 3.1 Connect Options

```typescript
interface SqlProofConnectOptions {
  connectionString?: string;  // Connect to an existing Postgres instance
  schema?: string;            // Schema name to introspect (default: 'public')
  schemaFile?: string;        // Auto-start testcontainers + execute this SQL file
}
```

Exactly one of `connectionString` or `schemaFile` must be provided.

### 3.2 SqlProof Class

```typescript
class SqlProof {
  /** Factory: connect to Postgres, introspect schema, return ready instance. */
  static async connect(options: SqlProofConnectOptions): Promise<SqlProof>

  /**
   * Register custom generators or FK distribution strategies for a table.
   * Returns `this` for fluent chaining.
   */
  customize(table: string, overrides: TableCustomization): this

  /** Run a property-based test. Throws SqlProofError on failure with counterexample. */
  async check(name: string, options: CheckOptions): Promise<void>

  /** Declarative shorthand: asserts the query returns 0 rows for all generated datasets. */
  async invariant(name: string, options: InvariantOptions): Promise<void>

  /** Close DB connection and stop the testcontainers instance (if auto-managed). */
  async disconnect(): Promise<void>
}
```

### 3.3 Check Options

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

### 3.4 Invariant Options

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

`invariant()` is a thin wrapper over `check()`: it converts `{ query, expectEmpty: true }`
into a `property` that runs the query and checks `rows.length === 0`.

### 3.5 Table Customization

```typescript
interface TableCustomization {
  /** FK distribution strategy per FK column name. */
  fkDistribution?: Record<string, FkDistributionStrategy>;
  /** Custom fast-check arbitrary per column name. */
  [columnName: string]: fc.Arbitrary<unknown> | Record<string, FkDistributionStrategy> | undefined;
}

type FkDistributionStrategy = 'zipf' | 'uniform' | 'adversarial';
```

Usage:

```typescript
proof.customize('products', {
  price: fc.float({ min: 0.01, max: 9999.99, noNaN: true }),
  name: fc.string({ minLength: 1, maxLength: 100 }),
});

proof.customize('orders', {
  fkDistribution: { customer_id: 'zipf' },
});
```

---

## 4. FK Distribution Strategies

Implemented as a new `distribution` parameter on `makeForeignKeyArbitrary()` in
`constraint-handler.ts`:

| Strategy | Implementation | Use case |
|---|---|---|
| `uniform` (default) | `fc.constantFrom(...ids)` | Equal probability per parent; good coverage |
| `zipf` | `fc.frequency({ weight: ⌈1000/(i+1)⌉, arbitrary: fc.constant(id) } for each id)` | Skewed: first parents get many children, later ones few or none; realistic |
| `adversarial` | `fc.constantFrom(first, middle, last)` | Boundary stress test: only picks edge-position parents |

The strategy flows: `customize()` → stored in `SqlProof.customizations` →
`makeTableArbitrary()` → `makeForeignKeyArbitrary(parentRows, refCol, strategy)`.

---

## 5. Data Flow

```
SqlProof.connect()
  → DBManager.start()           (start container or connect)
  → introspect schema            (executeAndIntrospect or introspectSchema)
  → store SchemaInfo

proof.customize('orders', {...})
  → store in customizations map

proof.check('name', { generate, property })
  → extract per-table rowCounts from generate{}
  → extract column overrides + fkDistribution from customizations
  → makeDatasetArbitrary(schemaInfo, rowCounts, overrides)
  → fc.assert(fc.asyncProperty(datasetArb, async dataset => {
      schemaName = uuid
      DBManager.setupSchema(schemaName, schemaInfo)
      DBManager.insertDataset(client, dataset, schemaInfo, schemaName)
      property(sqlProofClient)
    }))
  → on failure: formatCounterexample → throw SqlProofError

proof.disconnect()
  → DBManager.stop()
```

---

## 6. File Changes

| File | Change |
|---|---|
| `src/schema/types.ts` | Add `SqlProofConnectOptions`, `CheckOptions`, `InvariantOptions`, `TableCustomization`, `FkDistributionStrategy`; remove old `SqlProofCheckOptions` |
| `src/sqlproof.ts` | **New**: `SqlProof` class with `connect/check/invariant/customize/disconnect` |
| `src/runner/property-runner.ts` | Replace `runProperty()` with internal `runChecks()` accepting live DBManager + SchemaInfo |
| `src/generators/constraint-handler.ts` | Add `distribution` param to `makeForeignKeyArbitrary()`, implement zipf/adversarial |
| `src/generators/table-generator.ts` | Accept `fkDistribution` map, pass strategy per FK column |
| `src/generators/dataset-generator.ts` | Accept per-table row counts as `Record<string, number>` instead of flat `rowsPerTable` |
| `src/index.ts` | Export `SqlProof` class; remove `sqlproof` singleton export |
| `SPEC.md` | Update API documentation to match class-based API |
| `examples/orders/orders.test.ts` | Update to new API |
| `tests/integration/e2e.test.ts` | Update to new API |

---

## 7. Test Strategy (TDD)

Each change is test-driven in this order:

1. **`tests/generators/constraint-handler.test.ts`** — add tests for `makeForeignKeyArbitrary` with `zipf`, `uniform`, `adversarial` strategies
2. **`tests/generators/table-generator.test.ts`** — add tests for FK distribution flowing through table generation
3. **`tests/generators/dataset-generator.test.ts`** — add tests for per-table `rowCounts` map
4. **`tests/unit/sqlproof.test.ts`** — unit tests for `SqlProof` class (mock DBManager)
5. **`tests/integration/e2e.test.ts`** — integration tests using new class API
6. **`examples/orders/orders.test.ts`** — updated example using class API

---

## 8. Error Handling

- `SqlProof.connect()` throws if both or neither of `connectionString`/`schemaFile` are given.
- `proof.check()` throws `SqlProofError` on property failure with formatted counterexample.
- Insert failures (constraint violations during generation) are still silently skipped (logged with `console.warn`).

---

## 9. Non-Goals

- Backwards compatibility with the old `sqlproof.check()` flat API — this is removed.
- Circular FK support — still throws a clear error.
- Multi-column FK remapping — still single-column only.
