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
// Interfaces
// ---------------------------------------------------------------------------

export interface PgPool {
  query(sql: string, params?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
  connect(): Promise<PgClient>;
}

interface PgClient {
  query(sql: string, params?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
  release(): void;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Execute raw DDL in a disposable temporary schema, introspect it via
 * information_schema / pg_catalog, then drop the temp schema.
 * Postgres itself parses the DDL — no regex required.
 */
export async function executeAndIntrospect(
  pool: PgPool,
  sql: string,
): Promise<SchemaInfo> {
  const tmpSchema = `_sqlproof_introspect_${Math.random().toString(36).slice(2, 10)}`;
  const client = await pool.connect();
  try {
    await client.query(`CREATE SCHEMA "${tmpSchema}"`);
    await client.query(`SET search_path TO "${tmpSchema}"`);
    await client.query(sql);
    return await introspectSchema(pool, tmpSchema);
  } finally {
    await client.query(`SET search_path TO public`);
    await client.query(`DROP SCHEMA IF EXISTS "${tmpSchema}" CASCADE`);
    client.release();
  }
}

/**
 * Introspect an existing Postgres schema via information_schema and pg_catalog.
 */
export async function introspectSchema(
  pool: PgPool,
  schemaName: string = 'public',
): Promise<SchemaInfo> {
  const tablesRes = await pool.query(`
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = $1 AND table_type = 'BASE TABLE'
    ORDER BY table_name
  `, [schemaName]);
  const tableNames: string[] = tablesRes.rows.map(r => String(r['table_name']));

  const colsRes = await pool.query(`
    SELECT table_name, column_name, data_type, udt_name,
           is_nullable, column_default, is_generated, is_identity,
           character_maximum_length, numeric_precision, numeric_scale
    FROM information_schema.columns
    WHERE table_schema = $1
    ORDER BY table_name, ordinal_position
  `, [schemaName]);

  const pkRes = await pool.query(`
    SELECT kcu.table_name, kcu.column_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
    WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = $1
    ORDER BY kcu.table_name, kcu.ordinal_position
  `, [schemaName]);

  const fkRes = await pool.query(`
    SELECT kcu.table_name, kcu.column_name,
           ccu.table_name AS foreign_table_name, ccu.column_name AS foreign_column_name,
           tc.constraint_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
    JOIN information_schema.constraint_column_usage ccu
      ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
    WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = $1
    ORDER BY tc.constraint_name, kcu.ordinal_position
  `, [schemaName]);

  const uqRes = await pool.query(`
    SELECT kcu.table_name, kcu.column_name, tc.constraint_name
    FROM information_schema.table_constraints tc
    JOIN information_schema.key_column_usage kcu
      ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
    WHERE tc.constraint_type = 'UNIQUE' AND tc.table_schema = $1
    ORDER BY tc.constraint_name, kcu.ordinal_position
  `, [schemaName]);

  const chkRes = await pool.query(`
    SELECT tc.table_name, cc.check_clause
    FROM information_schema.table_constraints tc
    JOIN information_schema.check_constraints cc
      ON tc.constraint_name = cc.constraint_name AND tc.constraint_schema = cc.constraint_schema
    WHERE tc.constraint_type = 'CHECK' AND tc.table_schema = $1
      AND cc.check_clause NOT LIKE '% IS NOT NULL'
    ORDER BY tc.table_name
  `, [schemaName]);

  const enumRes = await pool.query(`
    SELECT t.typname AS enum_name, e.enumlabel AS enum_value
    FROM pg_catalog.pg_type t
    JOIN pg_catalog.pg_enum e ON t.oid = e.enumtypid
    JOIN pg_catalog.pg_namespace n ON t.typnamespace = n.oid
    WHERE n.nspname = $1
    ORDER BY t.typname, e.enumsortorder
  `, [schemaName]);

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
    is_identity: string;
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
      baseType = r.udt_name.startsWith('_') ? r.udt_name.slice(1) : r.udt_name;
      dataType = baseType;
    }

    const constraints: NumericConstraints = {};
    if (r.character_maximum_length != null) constraints.length = r.character_maximum_length;
    if (r.numeric_precision != null) constraints.precision = r.numeric_precision;
    if (r.numeric_scale != null) constraints.scale = r.numeric_scale;

    const isGenerated =
      r.is_generated === 'ALWAYS' ||
      r.is_identity === 'YES' ||
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
    arr.push(parseCheck(expr));
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

// ---------------------------------------------------------------------------
// CHECK expression parsing
// ---------------------------------------------------------------------------

function parseCheck(expr: string): CheckConstraint {
  const parsed = tryParseSimpleCheck(expr);
  return { expression: expr.trim(), ...(parsed ? { parsed } : {}) };
}

function tryParseSimpleCheck(expr: string): ParsedCheck | undefined {
  const e = expr.trim();

  // col BETWEEN x AND y
  const betweenMatch = e.match(
    /^"?(\w+)"?\s+BETWEEN\s+(-?[\d.]+)\s+AND\s+(-?[\d.]+)$/i,
  );
  if (betweenMatch) {
    return {
      column: betweenMatch[1]!,
      operator: 'BETWEEN',
      value: Number(betweenMatch[2]),
      value2: Number(betweenMatch[3]),
    };
  }

  // col IN ('a', 'b') or col IN (1, 2)
  const inMatch = e.match(/^"?(\w+)"?\s+IN\s*\((.+)\)$/i);
  if (inMatch) {
    const valuesRaw = inMatch[2]!;
    const values = splitValues(valuesRaw).map(v => {
      const t = v.trim();
      if (t.startsWith("'") && t.endsWith("'")) return t.slice(1, -1);
      const n = Number(t);
      return isNaN(n) ? t : n;
    });
    return { column: inMatch[1]!, operator: 'IN', value: values };
  }

  // col OP value
  const cmpMatch = e.match(/^"?(\w+)"?\s*(>=|<=|<>|!=|>|<|=)\s*(-?[\d.]+|'[^']*')$/);
  if (cmpMatch) {
    const opRaw = cmpMatch[2]!;
    if (opRaw === '<>' || opRaw === '!=') return undefined;
    const valRaw = cmpMatch[3]!.trim();
    const op = opRaw as '>' | '>=' | '<' | '<=' | '=';
    const value = valRaw.startsWith("'") ? valRaw.slice(1, -1) : Number(valRaw);
    return { column: cmpMatch[1]!, operator: op, value };
  }

  return undefined;
}

/**
 * Split comma-separated values respecting parentheses and string literals.
 */
function splitValues(body: string): string[] {
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
