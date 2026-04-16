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

export interface SqlProofConnectOptions {
  /** Connect to an existing Postgres instance. */
  connectionString?: string;
  /** Postgres schema name to introspect (default: 'public'). Only used with connectionString. */
  schema?: string;
  /** Path to a SQL DDL file. Auto-starts a testcontainers Postgres. */
  schemaFile?: string;
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

