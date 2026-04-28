import { Pool, type PoolClient } from 'pg';
import type { SchemaInfo, TableInfo, Dataset } from '../schema/types.js';
import { getInsertionOrder } from '../schema/dependency-graph.js';
import { rewriteDefaultValue } from './default-value-rewriter.js';

export interface DBManagerOptions {
  connectionString?: string;
  useTestcontainers?: boolean;
  neonBranchId?: string;
  neonOptions?: import('../schema/types.js').NeonOptions;
}

/**
 * Manages a PostgreSQL connection (either testcontainers or external).
 * Provides schema isolation per property run using CREATE/DROP SCHEMA.
 */
export class DBManager {
  private pool: Pool | null = null;
  private container: unknown = null;
  private options: DBManagerOptions;

  constructor(options: DBManagerOptions = {}) {
    this.options = options;
  }

  async start(): Promise<void> {
    if (this.options.connectionString) {
      this.pool = new Pool({ connectionString: this.options.connectionString });
    } else {
      // Dynamic import so users who provide connectionString don't need the dep
      const { PostgreSqlContainer } = await import('@testcontainers/postgresql');
      const container = await new PostgreSqlContainer('postgres:16')
        .withReuse()
        .start();
      this.container = container;
      this.pool = new Pool({ connectionString: container.getConnectionUri() });
    }
  }

  async setupSchema(schemaName: string, schemaInfo: SchemaInfo): Promise<void> {
    const pool = this.getPool();
    const client = await pool.connect();
    try {
      await client.query(`CREATE SCHEMA "${schemaName}"`);

      for (const enumInfo of schemaInfo.enums) {
        const values = enumInfo.values.map(v => `'${v.replace(/'/g, "''")}'`).join(', ');
        await client.query(
          `CREATE TYPE "${schemaName}"."${enumInfo.name}" AS ENUM (${values})`,
        );
      }

      const orderedTables = getInsertionOrder(schemaInfo.tables);
      for (const tableName of orderedTables) {
        const table = schemaInfo.tables.find(t => t.name === tableName);
        if (!table) continue;
        const sql = buildCreateTableSql(table, schemaName, schemaInfo);
        await client.query(sql);
      }
    } finally {
      client.release();
    }
  }

  async getClientForSchema(schemaName: string): Promise<PoolClient> {
    const pool = this.getPool();
    return pool.connect();
  }

  /**
   * Inserts the generated dataset in FK order and returns the real rows
   * (including DB-assigned serial IDs from RETURNING *).
   */
  async insertDataset(
    client: PoolClient,
    dataset: Dataset,
    schemaInfo: SchemaInfo,
    schemaName: string,
  ): Promise<Dataset> {
    const realDataset: Dataset = {};
    const orderedTables = getInsertionOrder(schemaInfo.tables);

    for (const tableName of orderedTables) {
      const rows = dataset[tableName];
      if (!rows || rows.length === 0) {
        realDataset[tableName] = [];
        continue;
      }

      const table = schemaInfo.tables.find(t => t.name === tableName);
      if (!table) continue;

      // Get non-generated columns for INSERT
      const insertCols = table.columns.filter(c => !c.isGenerated);

      // Remap FK column values to actual DB-assigned IDs from real parent rows
      const remappedRows = remapForeignKeys(rows, table, schemaInfo, dataset, realDataset);

      const realRows: Record<string, unknown>[] = [];

      for (const row of remappedRows) {
        const cols = insertCols.filter(c => row[c.name] !== undefined && row[c.name] !== null || !c.nullable === false);
        const nonNullCols = insertCols.filter(c => {
          const v = row[c.name];
          return v !== undefined && v !== null;
        });

        // Include all non-generated columns that have values
        const colsToInsert = insertCols.filter(c => {
          const v = row[c.name];
          return v !== undefined;
        });

        if (colsToInsert.length === 0) {
          // Insert a default row
          const res = await client.query(
            `INSERT INTO "${schemaName}"."${tableName}" DEFAULT VALUES RETURNING *`,
          );
          if (res.rows[0]) realRows.push(res.rows[0]);
          continue;
        }

        const colNames = colsToInsert.map(c => `"${c.name}"`).join(', ');
        const placeholders = colsToInsert.map((_, i) => `$${i + 1}`).join(', ');
        const values = colsToInsert.map(c => serializeForPg(row[c.name]));

        const sql = `INSERT INTO "${schemaName}"."${tableName}" (${colNames}) VALUES (${placeholders}) RETURNING *`;
        const res = await client.query(sql, values);
        if (res.rows[0]) realRows.push(res.rows[0]);
      }

      realDataset[tableName] = realRows;
    }

    return realDataset;
  }

  async dropSchema(schemaName: string): Promise<void> {
    const pool = this.getPool();
    const client = await pool.connect();
    try {
      await client.query(`DROP SCHEMA IF EXISTS "${schemaName}" CASCADE`);
    } finally {
      client.release();
    }
  }

  async stop(): Promise<void> {
    if (this.pool) {
      await this.pool.end();
      this.pool = null;
    }
    // Note: testcontainers container with .withReuse() is not stopped on purpose
    if (this.options.neonBranchId !== undefined && this.options.neonOptions !== undefined) {
      const { deleteNeonBranch } = await import('./neon-provider.js');
      const { apiKey, projectId } = this.options.neonOptions;
      await deleteNeonBranch(apiKey, projectId, this.options.neonBranchId).catch(err => {
        console.warn(
          `[sqlproof] Failed to delete Neon branch ${this.options.neonBranchId}: ${(err as Error).message}`,
        );
      });
    }
  }

  static generateSchemaName(): string {
    const id = Math.random().toString(36).slice(2, 10);
    return `run_${id}`;
  }

  getPool(): Pool {
    if (!this.pool) throw new Error('DBManager not started. Call start() first.');
    return this.pool;
  }
}

// ---------------------------------------------------------------------------
// SQL helpers
// ---------------------------------------------------------------------------

function serializeForPg(value: unknown): unknown {
  if (typeof value === 'bigint') return value.toString();
  return value;
}

/**
 * Rebuilds CREATE TABLE SQL for a table inside a specific schema.
 * Used when the schema was parsed from a SQL file (no original DDL available).
 */
function buildCreateTableSql(table: TableInfo, schemaName: string, schemaInfo: SchemaInfo): string {
  const lines: string[] = [];

  const enumNames = new Set(schemaInfo.enums.map(e => e.name.toLowerCase()));

  for (const col of table.columns) {
    let typePart = enumNames.has(col.dataType.toLowerCase())
      ? `"${schemaName}"."${col.dataType}"`
      : pgTypeForColumn(col);
    if (col.isGenerated) {
      typePart = serialType(col.dataType);
    }
    const nullPart = col.nullable ? '' : ' NOT NULL';
    const defaultPart =
      col.defaultValue && !col.isGenerated
        ? ` DEFAULT ${rewriteDefaultValue(col.defaultValue, schemaName, schemaInfo)}`
        : '';
    lines.push(`  "${col.name}" ${typePart}${nullPart}${defaultPart}`);
  }

  if (table.primaryKey.length > 0) {
    lines.push(`  PRIMARY KEY (${table.primaryKey.map(c => `"${c}"`).join(', ')})`);
  }

  for (const fk of table.foreignKeys) {
    const cols = fk.columns.map(c => `"${c}"`).join(', ');
    const refCols = fk.referencedColumns.map(c => `"${c}"`).join(', ');
    lines.push(
      `  FOREIGN KEY (${cols}) REFERENCES "${schemaName}"."${fk.referencedTable}" (${refCols})`,
    );
  }

  for (const uq of table.uniqueConstraints) {
    lines.push(`  UNIQUE (${uq.map(c => `"${c}"`).join(', ')})`);
  }

  for (const chk of table.checkConstraints) {
    lines.push(`  CHECK (${chk.expression})`);
  }

  return `CREATE TABLE "${schemaName}"."${table.name}" (\n${lines.join(',\n')}\n)`;
}

function pgTypeForColumn(col: TableInfo['columns'][number]): string {
  const t = col.dataType.toLowerCase();
  if (col.isArray) return `${pgBaseType(col.baseType ?? t)}[]`;
  if (t === 'numeric' || t === 'decimal') {
    const p = col.constraints.precision;
    const s = col.constraints.scale;
    if (p != null && s != null) return `NUMERIC(${p},${s})`;
    if (p != null) return `NUMERIC(${p})`;
    return 'NUMERIC';
  }
  if (t === 'varchar' || t === 'character varying') {
    const n = col.constraints.length;
    return n != null ? `VARCHAR(${n})` : 'TEXT';
  }
  if (t === 'char' || t === 'character' || t === 'bpchar') {
    const n = col.constraints.length ?? 1;
    return `CHAR(${n})`;
  }
  return pgBaseType(t);
}

function pgBaseType(t: string): string {
  switch (t.toLowerCase()) {
    case 'integer': case 'int4': case 'int': return 'INTEGER';
    case 'smallint': case 'int2': return 'SMALLINT';
    case 'bigint': case 'int8': return 'BIGINT';
    case 'serial': return 'SERIAL';
    case 'smallserial': return 'SMALLSERIAL';
    case 'bigserial': return 'BIGSERIAL';
    case 'real': case 'float4': return 'REAL';
    case 'double precision': case 'float8': return 'DOUBLE PRECISION';
    case 'boolean': case 'bool': return 'BOOLEAN';
    case 'text': return 'TEXT';
    case 'uuid': return 'UUID';
    case 'timestamp': case 'timestamp without time zone': return 'TIMESTAMP';
    case 'timestamptz': case 'timestamp with time zone': return 'TIMESTAMPTZ';
    case 'date': return 'DATE';
    case 'time': case 'time without time zone': return 'TIME';
    case 'timetz': case 'time with time zone': return 'TIMETZ';
    case 'json': return 'JSON';
    case 'jsonb': return 'JSONB';
    case 'bytea': return 'BYTEA';
    default: return t.toUpperCase();
  }
}

function serialType(dataType: string): string {
  const t = dataType.toLowerCase();
  if (t === 'bigserial' || t === 'serial8') return 'BIGSERIAL';
  if (t === 'smallserial' || t === 'serial2') return 'SMALLSERIAL';
  return 'SERIAL';
}

/**
 * Remaps FK column values from generated (pre-insert) IDs to the real DB-assigned IDs.
 * When a child table references a parent's PK, we need to map the generated value to
 * whatever the DB actually assigned (e.g. SERIAL).
 *
 * Strategy: if the parent table has a SERIAL PK, the generated dataset has placeholder
 * values (e.g. the generated row index). We match by position in the ordered parent rows.
 */
function remapForeignKeys(
  rows: Record<string, unknown>[],
  table: TableInfo,
  schema: SchemaInfo,
  generatedDataset: Dataset,
  realDataset: Dataset,
): Record<string, unknown>[] {
  if (table.foreignKeys.length === 0) return rows;

  return rows.map(row => {
    const remapped = { ...row };

    for (const fk of table.foreignKeys) {
      const parentTable = schema.tables.find(t => t.name === fk.referencedTable);
      if (!parentTable) continue;

      // Check if parent PK columns are generated (SERIAL)
      const parentHasGeneratedPk = fk.referencedColumns.some(refCol => {
        const col = parentTable.columns.find(c => c.name === refCol);
        return col?.isGenerated ?? false;
      });

      if (!parentHasGeneratedPk) continue;

      // Get the real parent rows with DB-assigned IDs
      const realParentRows = realDataset[fk.referencedTable];
      const genParentRows = generatedDataset[fk.referencedTable];
      if (!realParentRows || !genParentRows || realParentRows.length === 0) continue;

      if (fk.columns.length === 1) {
        const fkCol = fk.columns[0]!;
        const refCol = fk.referencedColumns[0]!;
        const genValue = remapped[fkCol];

        // The table generator emits 1-based placeholder indices for FK
        // columns that reference SERIAL PKs (since those columns are
        // absent from the generated rows). Convert directly to a
        // 0-based array index. For non-SERIAL PKs the generated parent
        // rows contain the actual value, so we fall back to a lookup.
        let parentIdx: number;
        const refColInfo = parentTable.columns.find(c => c.name === refCol);
        if (refColInfo?.isGenerated && typeof genValue === 'number') {
          parentIdx = genValue - 1;
        } else {
          parentIdx = genParentRows.findIndex(r => String(r[refCol]) === String(genValue));
        }

        if (parentIdx >= 0 && parentIdx < realParentRows.length && realParentRows[parentIdx]) {
          remapped[fkCol] = realParentRows[parentIdx]![refCol];
        }
      }
    }

    return remapped;
  });
}
