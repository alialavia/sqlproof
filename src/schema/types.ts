import type * as fc from 'fast-check';

export type PostgresType =
  | 'integer' | 'int4' | 'int' | 'smallint' | 'int2' | 'bigint' | 'int8'
  | 'serial' | 'smallserial' | 'bigserial'
  | 'real' | 'float4' | 'float8' | 'double precision'
  | 'numeric' | 'decimal'
  | 'boolean' | 'bool'
  | 'text' | 'varchar' | 'character varying' | 'char' | 'character' | 'bpchar'
  | 'uuid'
  | 'timestamp' | 'timestamp without time zone' | 'timestamptz' | 'timestamp with time zone'
  | 'date' | 'time' | 'time without time zone' | 'timetz' | 'time with time zone'
  | 'json' | 'jsonb'
  | 'bytea'
  | string; // fallback for enums and unknown types

export interface NumericConstraints {
  precision?: number;
  scale?: number;
  length?: number;
}

export interface ColumnInfo {
  name: string;
  dataType: PostgresType;
  nullable: boolean;
  defaultValue?: string;
  isGenerated: boolean;
  constraints: NumericConstraints;
  isArray: boolean;
  baseType?: string;
}

export interface ParsedCheck {
  column: string;
  operator: '>' | '>=' | '<' | '<=' | '=' | 'IN' | 'BETWEEN';
  value: unknown;
  value2?: unknown;
}

export interface CheckConstraint {
  expression: string;
  parsed?: ParsedCheck;
}

export interface ForeignKeyInfo {
  columns: string[];
  referencedTable: string;
  referencedColumns: string[];
}

export interface TableInfo {
  name: string;
  columns: ColumnInfo[];
  primaryKey: string[];
  foreignKeys: ForeignKeyInfo[];
  uniqueConstraints: string[][];
  checkConstraints: CheckConstraint[];
}

export interface EnumInfo {
  name: string;
  values: string[];
}

export interface SchemaInfo {
  tables: TableInfo[];
  enums: EnumInfo[];
}

export type Dataset = Record<string, Record<string, unknown>[]>;

export interface SqlProofClient {
  query(sql: string, params?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
  getGeneratedData(): Dataset;
}

export type FkDistributionStrategy = 'zipf' | 'uniform' | 'adversarial';

export interface TableCustomization {
  fkDistribution?: Record<string, FkDistributionStrategy>;
  [columnName: string]: fc.Arbitrary<unknown> | Record<string, FkDistributionStrategy> | undefined;
}

export interface NeonOptions {
  /** Neon API key (Neon Console → Account Settings → API Keys). */
  apiKey: string;
  /** Neon project ID. */
  projectId: string;
  /** Database name. Default: 'neondb'. */
  database?: string;
  /** Role name for the connection URI. Default: 'neondb_owner'. */
  role?: string;
  /** Parent branch name or ID to branch from. Default: project default branch. */
  parentBranch?: string;
}

export interface SqlProofConnectOptions {
  /** Connect to an existing Postgres instance. */
  connectionString?: string;
  /** Postgres schema name to introspect (default: 'public'). Used with `connectionString` or `neon`. */
  schema?: string;
  /**
   * Path to a SQL DDL file.
   * - Without `connectionString`: auto-starts a testcontainers Postgres (requires Docker).
   * - With `connectionString`: applies DDL to a temp schema on the provided DB — no Docker needed.
   */
  schemaFile?: string;
  /** Use Neon branching for instant isolated test databases. Cannot be combined with `connectionString` or `schemaFile`. */
  neon?: NeonOptions;
}

export interface CheckOptions {
  /** Per-table row counts, e.g. { customers: 20, orders: 100, line_items: 500 } */
  generate: Record<string, number>;
  /** Optional mutations to run after data insertion, before the property. */
  setup?: (db: SqlProofClient) => Promise<void>;
  /** Returns true if the property holds, false if violated. */
  property: (db: SqlProofClient) => Promise<boolean>;
  /** Number of random datasets to test. Default: 100. */
  runs?: number;
  /** Seed for reproducible failures. */
  seed?: number;
  /** Per-run timeout in ms. Default: 5000. */
  timeout?: number;
}

export interface InvariantOptions {
  /** Per-table row counts. */
  generate: Record<string, number>;
  /** SQL query whose result must be empty for the invariant to hold. */
  query: string;
  expectEmpty: true;
  runs?: number;
  seed?: number;
  timeout?: number;
}

