import { readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import type {
  SchemaInfo,
  TableInfo,
  ColumnInfo,
  ForeignKeyInfo,
  CheckConstraint,
  ParsedCheck,
  EnumInfo,
  NumericConstraints,
} from './types.js';

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export async function parseSchemaFromFile(filePath: string): Promise<SchemaInfo> {
  const sql = await readFile(filePath, 'utf8');
  return parseSchemaFromSql(sql);
}

export async function parseSchemaFromConnection(connectionString: string): Promise<SchemaInfo> {
  // Dynamically import pg to support both ESM and CJS
  const pg = await import('pg');
  const { Pool } = pg.default ?? pg;
  const pool = new Pool({ connectionString });
  try {
    return await introspectSchema(pool);
  } finally {
    await pool.end();
  }
}

/** Synchronous entry point used in unit tests (no file I/O, no DB). */
export function parseSchemaFromSql(sql: string): SchemaInfo {
  const cleaned = stripComments(sql);
  const enums = extractEnums(cleaned);
  const tables = extractTables(cleaned, enums);
  return { tables, enums };
}

// ---------------------------------------------------------------------------
// Comment stripping
// ---------------------------------------------------------------------------

function stripComments(sql: string): string {
  // Remove /* ... */ block comments (non-greedy, handle nesting manually)
  let result = '';
  let i = 0;
  while (i < sql.length) {
    if (sql[i] === '/' && sql[i + 1] === '*') {
      let depth = 1;
      i += 2;
      while (i < sql.length && depth > 0) {
        if (sql[i] === '/' && sql[i + 1] === '*') { depth++; i += 2; }
        else if (sql[i] === '*' && sql[i + 1] === '/') { depth--; i += 2; }
        else { i++; }
      }
      result += ' ';
    } else if (sql[i] === '-' && sql[i + 1] === '-') {
      // Line comment — skip to end of line
      while (i < sql.length && sql[i] !== '\n') i++;
    } else {
      result += sql[i];
      i++;
    }
  }
  return result;
}

// ---------------------------------------------------------------------------
// Enum extraction
// ---------------------------------------------------------------------------

function extractEnums(sql: string): EnumInfo[] {
  const enums: EnumInfo[] = [];
  // CREATE TYPE name AS ENUM (...)
  const enumRe = /CREATE\s+TYPE\s+(\w+)\s+AS\s+ENUM\s*\(/gi;
  let match: RegExpExecArray | null;
  while ((match = enumRe.exec(sql)) !== null) {
    const name = match[1]!;
    const openParen = match.index + match[0].length - 1;
    const body = extractParenBody(sql, openParen);
    const values = parseEnumValues(body);
    enums.push({ name, values });
  }
  return enums;
}

function parseEnumValues(body: string): string[] {
  return body
    .split(',')
    .map(s => s.trim())
    .filter(s => s.startsWith("'") && s.endsWith("'"))
    .map(s => s.slice(1, -1));
}

// ---------------------------------------------------------------------------
// Table extraction
// ---------------------------------------------------------------------------

function extractTables(sql: string, enums: EnumInfo[]): TableInfo[] {
  const tables: TableInfo[] = [];
  // CREATE TABLE [IF NOT EXISTS] name (
  const tableRe = /CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"?(\w+)"?\s*\.\s*)?"?(\w+)"?\s*\(/gi;
  let match: RegExpExecArray | null;
  while ((match = tableRe.exec(sql)) !== null) {
    const tableName = match[2]!;
    const openParen = match.index + match[0].length - 1;
    const body = extractParenBody(sql, openParen);
    const table = parseTableBody(tableName, body, enums);
    tables.push(table);
  }
  return tables;
}

/**
 * Parenthesis-depth scanner: given the index of '(' in sql, returns the
 * content between the outermost matching parens (exclusive).
 */
function extractParenBody(sql: string, openParenIndex: number): string {
  let depth = 0;
  let start = -1;
  let i = openParenIndex;
  let inString = false;
  let stringChar = '';

  while (i < sql.length) {
    const ch = sql[i]!;

    if (inString) {
      if (ch === stringChar) {
        // Handle escaped quotes ''
        if (stringChar === "'" && sql[i + 1] === "'") { i += 2; continue; }
        inString = false;
      }
      i++;
      continue;
    }

    if (ch === "'" || ch === '"') {
      inString = true;
      stringChar = ch;
    } else if (ch === '(') {
      depth++;
      if (depth === 1) start = i + 1;
    } else if (ch === ')') {
      depth--;
      if (depth === 0) return sql.slice(start, i);
    }
    i++;
  }
  return sql.slice(start === -1 ? openParenIndex + 1 : start);
}

/**
 * Split table body at depth-0 commas, respecting parens and string literals.
 */
function splitAtDepthZeroCommas(body: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let inString = false;
  let stringChar = '';
  let start = 0;

  for (let i = 0; i < body.length; i++) {
    const ch = body[i]!;

    if (inString) {
      if (ch === stringChar) {
        if (stringChar === "'" && body[i + 1] === "'") { i++; continue; }
        inString = false;
      }
      continue;
    }

    if (ch === "'" || ch === '"') {
      inString = true;
      stringChar = ch;
    } else if (ch === '(') {
      depth++;
    } else if (ch === ')') {
      depth--;
    } else if (ch === ',' && depth === 0) {
      parts.push(body.slice(start, i).trim());
      start = i + 1;
    }
  }

  const last = body.slice(start).trim();
  if (last.length > 0) parts.push(last);
  return parts;
}

// ---------------------------------------------------------------------------
// Table body parsing
// ---------------------------------------------------------------------------

function parseTableBody(tableName: string, body: string, enums: EnumInfo[]): TableInfo {
  const parts = splitAtDepthZeroCommas(body);
  const columns: ColumnInfo[] = [];
  const primaryKeyColumns: string[] = [];
  const foreignKeys: ForeignKeyInfo[] = [];
  const uniqueConstraints: string[][] = [];
  const checkConstraints: CheckConstraint[] = [];
  const inlinePrimaryKeys: string[] = [];

  for (const part of parts) {
    const trimmed = part.trim();
    if (!trimmed) continue;

    const upper = trimmed.toUpperCase();

    // Table-level constraints
    if (upper.startsWith('PRIMARY KEY')) {
      const cols = extractColumnList(trimmed);
      primaryKeyColumns.push(...cols);
    } else if (upper.startsWith('FOREIGN KEY')) {
      const fk = parseForeignKeyConstraint(trimmed);
      if (fk) foreignKeys.push(fk);
    } else if (upper.startsWith('UNIQUE')) {
      const cols = extractColumnList(trimmed);
      if (cols.length > 0) uniqueConstraints.push(cols);
    } else if (upper.startsWith('CHECK')) {
      const expr = extractCheckExpression(trimmed);
      if (expr) checkConstraints.push(parseCheck(expr, columns.map(c => c.name)));
    } else if (upper.startsWith('CONSTRAINT')) {
      // Named constraint: CONSTRAINT name PRIMARY KEY / FOREIGN KEY / UNIQUE / CHECK
      const withoutName = trimmed.replace(/^CONSTRAINT\s+\S+\s+/i, '').trim();
      const wu = withoutName.toUpperCase();
      if (wu.startsWith('PRIMARY KEY')) {
        primaryKeyColumns.push(...extractColumnList(withoutName));
      } else if (wu.startsWith('FOREIGN KEY')) {
        const fk = parseForeignKeyConstraint(withoutName);
        if (fk) foreignKeys.push(fk);
      } else if (wu.startsWith('UNIQUE')) {
        const cols = extractColumnList(withoutName);
        if (cols.length > 0) uniqueConstraints.push(cols);
      } else if (wu.startsWith('CHECK')) {
        const expr = extractCheckExpression(withoutName);
        if (expr) checkConstraints.push(parseCheck(expr, columns.map(c => c.name)));
      }
    } else {
      // Column definition
      const col = parseColumnDef(trimmed, enums);
      if (col) {
        columns.push(col.column);
        if (col.isPrimaryKey) inlinePrimaryKeys.push(col.column.name);
        if (col.foreignKey) foreignKeys.push(col.foreignKey);
        if (col.unique) uniqueConstraints.push([col.column.name]);
        if (col.check) checkConstraints.push(col.check);
      }
    }
  }

  const allPrimaryKeys = primaryKeyColumns.length > 0 ? primaryKeyColumns : inlinePrimaryKeys;

  return {
    name: tableName,
    columns,
    primaryKey: allPrimaryKeys,
    foreignKeys,
    uniqueConstraints,
    checkConstraints,
  };
}

// ---------------------------------------------------------------------------
// Column definition parsing
// ---------------------------------------------------------------------------

interface ParsedColumn {
  column: ColumnInfo;
  isPrimaryKey: boolean;
  foreignKey?: ForeignKeyInfo | undefined;
  unique?: boolean | undefined;
  check?: CheckConstraint | undefined;
}

function parseColumnDef(def: string, enums: EnumInfo[]): ParsedColumn | null {
  // Column name (optionally quoted)
  const nameMatch = def.match(/^"?(\w+)"?\s+/);
  if (!nameMatch) return null;
  const name = nameMatch[1]!;

  let remainder = def.slice(nameMatch[0].length).trim();

  // Parse data type (potentially multi-word)
  const { dataType, isArray, baseType, constraints: numConstraints, remainder: afterType } =
    parseDataType(remainder, enums);

  remainder = afterType.trim();
  const upperRem = remainder.toUpperCase();

  const isGenerated =
    /\bSERIAL\b/i.test(dataType) ||
    /\bSMALLSERIAL\b/i.test(dataType) ||
    /\bBIGSERIAL\b/i.test(dataType) ||
    /\bGENERATED\s+(ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY\b/i.test(remainder);

  const nullable = !/\bNOT\s+NULL\b/i.test(remainder);

  // DEFAULT value
  let defaultValue: string | undefined;
  const defaultMatch = remainder.match(/\bDEFAULT\s+(.+?)(?:\s+(?:NOT\s+NULL|NULL|UNIQUE|PRIMARY|REFERENCES|CHECK|GENERATED)\b|$)/i);
  if (defaultMatch) defaultValue = defaultMatch[1]!.trim();

  let isPrimaryKey = /\bPRIMARY\s+KEY\b/i.test(remainder);
  let unique = /\bUNIQUE\b/i.test(remainder);

  // Inline REFERENCES
  let foreignKey: ForeignKeyInfo | undefined;
  const refMatch = remainder.match(/\bREFERENCES\s+"?(\w+)"?\s*(?:\(\s*"?(\w+)"?\s*\))?/i);
  if (refMatch) {
    const refTable = refMatch[1]!;
    const refCol = refMatch[2] ?? 'id';
    foreignKey = { columns: [name], referencedTable: refTable, referencedColumns: [refCol] };
  }

  // Inline CHECK
  let check: CheckConstraint | undefined;
  const checkMatch = remainder.match(/\bCHECK\s*\(/i);
  if (checkMatch) {
    const parenStart = remainder.indexOf('(', checkMatch.index!);
    const expr = extractParenBody(remainder, parenStart);
    check = parseCheck(expr, [name]);
  }

  const column: ColumnInfo = {
    name,
    dataType,
    nullable,
    isGenerated,
    constraints: numConstraints,
    isArray,
    ...(defaultValue !== undefined ? { defaultValue } : {}),
    ...(baseType !== undefined ? { baseType } : {}),
  };

  return {
    column,
    isPrimaryKey,
    unique,
    ...(foreignKey !== undefined ? { foreignKey } : {}),
    ...(check !== undefined ? { check } : {}),
  };
}

interface TypeParseResult {
  dataType: string;
  isArray: boolean;
  baseType?: string;
  constraints: NumericConstraints;
  remainder: string;
}

function parseDataType(s: string, enums: EnumInfo[]): TypeParseResult {
  const upper = s.toUpperCase();
  let constraints: NumericConstraints = {};

  // Multi-word types first
  const multiWord = [
    'TIMESTAMP WITH TIME ZONE',
    'TIMESTAMP WITHOUT TIME ZONE',
    'TIME WITH TIME ZONE',
    'TIME WITHOUT TIME ZONE',
    'DOUBLE PRECISION',
    'CHARACTER VARYING',
    'BIT VARYING',
  ];

  for (const mw of multiWord) {
    if (upper.startsWith(mw)) {
      const remainder = s.slice(mw.length);
      // Check for array suffix
      const arrMatch = remainder.match(/^\s*\[\s*\]/);
      if (arrMatch) {
        return {
          dataType: mw.toLowerCase(),
          isArray: true,
          baseType: mw.toLowerCase(),
          constraints,
          remainder: remainder.slice(arrMatch[0].length),
        };
      }
      return { dataType: mw.toLowerCase(), isArray: false, constraints, remainder };
    }
  }

  // TYPE(p,s) or TYPE(n)
  const typeWithParens = s.match(/^(\w+)\s*\(([^)]+)\)/i);
  if (typeWithParens) {
    const typeName = typeWithParens[1]!.toLowerCase();
    const params = typeWithParens[2]!.split(',').map(p => parseInt(p.trim(), 10));
    if (params.length === 2 && params[0] != null && params[1] != null) {
      constraints = { precision: params[0], scale: params[1] };
    } else if (params.length === 1 && params[0] != null) {
      if (['varchar', 'character varying', 'char', 'character', 'bpchar'].includes(typeName)) {
        constraints = { length: params[0] };
      } else {
        constraints = { precision: params[0] };
      }
    }
    const afterParen = s.slice(typeWithParens[0].length);
    const arrMatch = afterParen.match(/^\s*\[\s*\]/);
    if (arrMatch) {
      return {
        dataType: typeName,
        isArray: true,
        baseType: typeName,
        constraints,
        remainder: afterParen.slice(arrMatch[0].length),
      };
    }
    return { dataType: typeName, isArray: false, constraints, remainder: afterParen };
  }

  // Simple type
  const simpleMatch = s.match(/^(\w+)/i);
  if (!simpleMatch) {
    return { dataType: 'text', isArray: false, constraints, remainder: s };
  }
  const typeName = simpleMatch[1]!.toLowerCase();
  const afterType = s.slice(simpleMatch[0].length);

  // Array suffix: integer[] or integer ARRAY
  const arrBracket = afterType.match(/^\s*\[\s*\]/);
  const arrKeyword = afterType.match(/^\s+ARRAY\b/i);

  if (arrBracket) {
    return {
      dataType: typeName,
      isArray: true,
      baseType: typeName,
      constraints,
      remainder: afterType.slice(arrBracket[0].length),
    };
  }
  if (arrKeyword) {
    return {
      dataType: typeName,
      isArray: true,
      baseType: typeName,
      constraints,
      remainder: afterType.slice(arrKeyword[0].length),
    };
  }

  return { dataType: typeName, isArray: false, constraints, remainder: afterType };
}

// ---------------------------------------------------------------------------
// Constraint parsing helpers
// ---------------------------------------------------------------------------

function extractColumnList(def: string): string[] {
  const parenMatch = def.match(/\(([^)]+)\)/);
  if (!parenMatch) return [];
  return parenMatch[1]!
    .split(',')
    .map(s => s.trim().replace(/^"|"$/g, ''));
}

function parseForeignKeyConstraint(def: string): ForeignKeyInfo | null {
  // FOREIGN KEY (col1, col2) REFERENCES table (col1, col2)
  const match = def.match(
    /FOREIGN\s+KEY\s*\(([^)]+)\)\s+REFERENCES\s+"?(\w+)"?\s*(?:\(([^)]+)\))?/i,
  );
  if (!match) return null;
  const columns = match[1]!.split(',').map(s => s.trim().replace(/^"|"$/g, ''));
  const referencedTable = match[2]!;
  const referencedColumns = match[3]
    ? match[3].split(',').map(s => s.trim().replace(/^"|"$/g, ''))
    : ['id'];
  return { columns, referencedTable, referencedColumns };
}

function extractCheckExpression(def: string): string | null {
  const parenIdx = def.toUpperCase().indexOf('CHECK');
  if (parenIdx === -1) return null;
  const openParen = def.indexOf('(', parenIdx);
  if (openParen === -1) return null;
  return extractParenBody(def, openParen);
}

// ---------------------------------------------------------------------------
// CHECK expression parsing
// ---------------------------------------------------------------------------

function parseCheck(expr: string, colNames: string[]): CheckConstraint {
  const parsed = tryParseSimpleCheck(expr, colNames);
  return { expression: expr.trim(), ...(parsed ? { parsed } : {}) };
}

function tryParseSimpleCheck(expr: string, colNames: string[]): ParsedCheck | undefined {
  const e = expr.trim();

  // col BETWEEN x AND y
  const betweenMatch = e.match(
    /^"?(\w+)"?\s+BETWEEN\s+(-?[\d.]+)\s+AND\s+(-?[\d.]+)$/i,
  );
  if (betweenMatch) {
    const col = betweenMatch[1]!;
    if (colNames.length === 0 || colNames.includes(col)) {
      return {
        column: col,
        operator: 'BETWEEN',
        value: Number(betweenMatch[2]),
        value2: Number(betweenMatch[3]),
      };
    }
  }

  // col IN ('a', 'b') or col IN (1, 2)
  const inMatch = e.match(/^"?(\w+)"?\s+IN\s*\((.+)\)$/i);
  if (inMatch) {
    const col = inMatch[1]!;
    if (colNames.length === 0 || colNames.includes(col)) {
      const valuesRaw = inMatch[2]!;
      const values = splitAtDepthZeroCommas(valuesRaw).map(v => {
        const t = v.trim();
        if (t.startsWith("'") && t.endsWith("'")) return t.slice(1, -1);
        const n = Number(t);
        return isNaN(n) ? t : n;
      });
      return { column: col, operator: 'IN', value: values };
    }
  }

  // col OP value
  const cmpMatch = e.match(/^"?(\w+)"?\s*(>=|<=|<>|!=|>|<|=)\s*(-?[\d.]+|'[^']*')$/);
  if (cmpMatch) {
    const col = cmpMatch[1]!;
    if (colNames.length === 0 || colNames.includes(col)) {
      const opRaw = cmpMatch[2]!;
      if (opRaw === '<>' || opRaw === '!=') return undefined;
      const valRaw = cmpMatch[3]!.trim();
      const op = opRaw as '>' | '>=' | '<' | '<=' | '=';
      const value = valRaw.startsWith("'") ? valRaw.slice(1, -1) : Number(valRaw);
      return { column: col, operator: op, value };
    }
  }

  return undefined;
}

// ---------------------------------------------------------------------------
// Connection-string introspection
// ---------------------------------------------------------------------------

interface PgPool {
  query(sql: string, params?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
}

async function introspectSchema(pool: PgPool): Promise<SchemaInfo> {
  // Tables
  const tablesRes = await pool.query(`
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    ORDER BY table_name
  `);
  const tableNames: string[] = tablesRes.rows.map(r => String(r['table_name']));

  // Columns
  const colsRes = await pool.query(`
    SELECT table_name, column_name, data_type, udt_name,
           is_nullable, column_default, is_generated,
           character_maximum_length, numeric_precision, numeric_scale
    FROM information_schema.columns
    WHERE table_schema = 'public'
    ORDER BY table_name, ordinal_position
  `);

  // Primary keys
  const pkRes = await pool.query(`
    SELECT kcu.table_name, kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
    WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'
    ORDER BY kcu.table_name, kcu.ordinal_position
  `);

  // Foreign keys
  const fkRes = await pool.query(`
    SELECT kcu.table_name, kcu.column_name,
           ccu.table_name AS foreign_table_name, ccu.column_name AS foreign_column_name,
           tc.constraint_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
    JOIN information_schema.constraint_column_usage ccu
      ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
    WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
    ORDER BY tc.constraint_name, kcu.ordinal_position
  `);

  // Unique constraints
  const uqRes = await pool.query(`
    SELECT kcu.table_name, kcu.column_name, tc.constraint_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
    WHERE tc.constraint_type = 'UNIQUE' AND tc.table_schema = 'public'
    ORDER BY tc.constraint_name, kcu.ordinal_position
  `);

  // Check constraints (exclude IS NOT NULL auto-generated)
  const chkRes = await pool.query(`
    SELECT tc.table_name, cc.check_clause
    FROM information_schema.table_constraints tc
    JOIN information_schema.check_constraints cc
      ON tc.constraint_name = cc.constraint_name AND tc.constraint_schema = cc.constraint_schema
    WHERE tc.constraint_type = 'CHECK' AND tc.table_schema = 'public'
      AND cc.check_clause NOT LIKE '% IS NOT NULL'
    ORDER BY tc.table_name
  `);

  // Enum values from pg_catalog
  const enumRes = await pool.query(`
    SELECT t.typname AS enum_name, e.enumlabel AS enum_value
    FROM pg_catalog.pg_type t
    JOIN pg_catalog.pg_enum e ON t.oid = e.enumtypid
    JOIN pg_catalog.pg_namespace n ON t.typnamespace = n.oid
    WHERE n.nspname = 'public'
    ORDER BY t.typname, e.enumsortorder
  `);

  // Build enum map
  const enumMap = new Map<string, string[]>();
  for (const row of enumRes.rows) {
    const name = String(row['enum_name']);
    const val = String(row['enum_value']);
    const arr = enumMap.get(name) ?? [];
    arr.push(val);
    enumMap.set(name, arr);
  }
  const enums: EnumInfo[] = Array.from(enumMap.entries()).map(([name, values]) => ({ name, values }));

  // Build column map
  type ColRow = {
    table_name: string;
    column_name: string;
    data_type: string;
    udt_name: string;
    is_nullable: string;
    column_default: string | null;
    is_generated: string;
    character_maximum_length: number | null;
    numeric_precision: number | null;
    numeric_scale: number | null;
  };

  const colMap = new Map<string, ColumnInfo[]>();
  for (const r of colsRes.rows as ColRow[]) {
    const tname = r.table_name;
    let dataType = r.data_type === 'USER-DEFINED' ? r.udt_name : r.data_type;

    const isArray = dataType === 'ARRAY';
    let baseType: string | undefined;
    if (isArray) {
      // udt_name for array types starts with '_'
      baseType = r.udt_name.startsWith('_') ? r.udt_name.slice(1) : r.udt_name;
      dataType = baseType;
    }

    const constraints: NumericConstraints = {};
    if (r.character_maximum_length != null) constraints.length = r.character_maximum_length;
    if (r.numeric_precision != null) constraints.precision = r.numeric_precision;
    if (r.numeric_scale != null) constraints.scale = r.numeric_scale;

    const isGenerated =
      r.is_generated === 'ALWAYS' ||
      (r.column_default != null &&
        /^nextval\(/i.test(r.column_default));

    const col: ColumnInfo = {
      name: r.column_name,
      dataType,
      nullable: r.is_nullable === 'YES',
      isGenerated,
      constraints,
      isArray,
      ...(r.column_default != null ? { defaultValue: r.column_default } : {}),
      ...(baseType !== undefined ? { baseType } : {}),
    };

    const arr = colMap.get(tname) ?? [];
    arr.push(col);
    colMap.set(tname, arr);
  }

  // Primary keys map
  const pkMap = new Map<string, string[]>();
  for (const r of pkRes.rows) {
    const tname = String(r['table_name']);
    const col = String(r['column_name']);
    const arr = pkMap.get(tname) ?? [];
    arr.push(col);
    pkMap.set(tname, arr);
  }

  // Foreign keys map
  const fkMapByConstraint = new Map<string, ForeignKeyInfo>();
  const fkOrder = new Map<string, string>();
  for (const r of fkRes.rows) {
    const cname = String(r['constraint_name']);
    const tname = String(r['table_name']);
    const col = String(r['column_name']);
    const refTable = String(r['foreign_table_name']);
    const refCol = String(r['foreign_column_name']);
    if (!fkMapByConstraint.has(cname)) {
      fkMapByConstraint.set(cname, { columns: [], referencedTable: refTable, referencedColumns: [] });
      fkOrder.set(cname, tname);
    }
    fkMapByConstraint.get(cname)!.columns.push(col);
    fkMapByConstraint.get(cname)!.referencedColumns.push(refCol);
  }

  const fksByTable = new Map<string, ForeignKeyInfo[]>();
  for (const [cname, fk] of fkMapByConstraint) {
    const tname = fkOrder.get(cname)!;
    const arr = fksByTable.get(tname) ?? [];
    arr.push(fk);
    fksByTable.set(tname, arr);
  }

  // Unique constraints map
  const uqByConstraint = new Map<string, string[]>();
  const uqTable = new Map<string, string>();
  for (const r of uqRes.rows) {
    const cname = String(r['constraint_name']);
    const tname = String(r['table_name']);
    const col = String(r['column_name']);
    if (!uqByConstraint.has(cname)) {
      uqByConstraint.set(cname, []);
      uqTable.set(cname, tname);
    }
    uqByConstraint.get(cname)!.push(col);
  }

  const uqByTable = new Map<string, string[][]>();
  for (const [cname, cols] of uqByConstraint) {
    const tname = uqTable.get(cname)!;
    const arr = uqByTable.get(tname) ?? [];
    arr.push(cols);
    uqByTable.set(tname, arr);
  }

  // Check constraints map
  const chkByTable = new Map<string, CheckConstraint[]>();
  for (const r of chkRes.rows) {
    const tname = String(r['table_name']);
    const expr = String(r['check_clause']);
    const arr = chkByTable.get(tname) ?? [];
    arr.push(parseCheck(expr, []));
    chkByTable.set(tname, arr);
  }

  // Assemble tables
  const tables: TableInfo[] = tableNames.map(tname => ({
    name: tname,
    columns: colMap.get(tname) ?? [],
    primaryKey: pkMap.get(tname) ?? [],
    foreignKeys: fksByTable.get(tname) ?? [],
    uniqueConstraints: uqByTable.get(tname) ?? [],
    checkConstraints: chkByTable.get(tname) ?? [],
  }));

  return { tables, enums };
}
