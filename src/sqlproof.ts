import { readFile } from 'node:fs/promises';
import type {
  SchemaInfo,
  SqlProofConnectOptions,
  CheckOptions,
  InvariantOptions,
  TableCustomization,
} from './schema/types.js';
import { executeAndIntrospect, introspectSchema } from './schema/parser.js';
import { DBManager } from './runner/db-manager.js';
import { runChecks } from './runner/property-runner.js';

export class SqlProof {
  private readonly dbManager: DBManager;
  private readonly schemaInfo: SchemaInfo;
  private readonly customizations: Map<string, TableCustomization>;

  private constructor(dbManager: DBManager, schemaInfo: SchemaInfo) {
    this.dbManager = dbManager;
    this.schemaInfo = schemaInfo;
    this.customizations = new Map();
  }

  /**
   * Connect to Postgres and introspect the schema.
   * Provide either `connectionString` (existing DB) or `schemaFile` (auto-managed container).
   */
  static async connect(options: SqlProofConnectOptions): Promise<SqlProof> {
    const { connectionString, schema = 'public', schemaFile } = options;

    if (!connectionString && !schemaFile) {
      throw new Error(
        'SqlProof.connect: provide either `connectionString` or `schemaFile`, not neither.',
      );
    }
    if (connectionString && schemaFile) {
      throw new Error(
        'SqlProof.connect: provide either `connectionString` or `schemaFile`, not both.',
      );
    }

    const dbManager = new DBManager(
      connectionString ? { connectionString } : { useTestcontainers: true },
    );
    await dbManager.start();

    let schemaInfo: SchemaInfo;
    if (schemaFile) {
      const sql = await readFile(schemaFile, 'utf8');
      schemaInfo = await executeAndIntrospect(dbManager.getPool(), sql);
    } else {
      schemaInfo = await introspectSchema(dbManager.getPool(), schema);
    }

    return new SqlProof(dbManager, schemaInfo);
  }

  /**
   * Register custom column generators or FK distribution strategies for a table.
   * Returns `this` for fluent chaining.
   */
  customize(table: string, overrides: TableCustomization): this {
    const existing = this.customizations.get(table) ?? {};
    this.customizations.set(table, { ...existing, ...overrides });
    return this;
  }

  /**
   * Run a property-based test. Throws `SqlProofError` on failure with a
   * formatted counterexample.
   */
  async check(name: string, options: CheckOptions): Promise<void> {
    await runChecks(name, options, this.dbManager, this.schemaInfo, this.customizations);
  }

  /**
   * Declarative shorthand: asserts the given SQL query returns 0 rows for
   * every generated dataset.
   */
  async invariant(name: string, options: InvariantOptions): Promise<void> {
    const { generate, query, runs, seed, timeout } = options;
    await this.check(name, {
      generate,
      runs,
      seed,
      timeout,
      property: async db => {
        const result = await db.query(query);
        return result.rows.length === 0;
      },
    });
  }

  /**
   * Close the DB connection and stop the testcontainers instance (if auto-managed).
   */
  async disconnect(): Promise<void> {
    await this.dbManager.stop();
  }
}
