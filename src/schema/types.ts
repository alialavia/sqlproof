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

export interface GeneratorOverrides {
  [tableName: string]: { [columnName: string]: fc.Arbitrary<unknown> };
}

export type FkDistributionStrategy = 'zipf' | 'uniform' | 'adversarial';

export interface TableCustomization {
  fkDistribution?: Record<string, FkDistributionStrategy>;
  [columnName: string]: fc.Arbitrary<unknown> | Record<string, FkDistributionStrategy> | undefined;
}

export interface SqlProofCheckOptions {
  name: string;
  schema: string;
  property: (db: SqlProofClient) => Promise<boolean>;
  runs?: number;
  rowsPerTable?: number;
  seed?: number;
  timeout?: number;
  tables?: string[];
  overrides?: GeneratorOverrides;
}
