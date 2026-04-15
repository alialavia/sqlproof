import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { makeTableArbitrary } from '../../src/generators/table-generator.js';
import type { TableInfo, SchemaInfo, Dataset } from '../../src/schema/types.js';

function col(
  name: string,
  dataType: string,
  opts: { nullable?: boolean; isGenerated?: boolean; precision?: number; scale?: number } = {},
) {
  return {
    name,
    dataType,
    nullable: opts.nullable ?? false,
    isGenerated: opts.isGenerated ?? false,
    constraints: {
      ...(opts.precision != null ? { precision: opts.precision } : {}),
      ...(opts.scale != null ? { scale: opts.scale } : {}),
    },
    isArray: false,
  };
}

// ---------------------------------------------------------------------------
// Basic row generation
// ---------------------------------------------------------------------------

describe('makeTableArbitrary', () => {
  it('generates the correct number of rows', () => {
    const table: TableInfo = {
      name: 'users',
      columns: [col('name', 'text')],
      primaryKey: ['name'],
      foreignKeys: [],
      uniqueConstraints: [],
      checkConstraints: [],
    };
    const schema: SchemaInfo = { tables: [table], enums: [] };

    const arb = makeTableArbitrary(table, schema, {}, 5);
    fc.assert(
      fc.property(arb, rows => rows.length === 5),
    );
  });

  it('skips generated (SERIAL) columns', () => {
    const table: TableInfo = {
      name: 'items',
      columns: [
        col('id', 'serial', { isGenerated: true }),
        col('label', 'text'),
      ],
      primaryKey: ['id'],
      foreignKeys: [],
      uniqueConstraints: [],
      checkConstraints: [],
    };
    const schema: SchemaInfo = { tables: [table], enums: [] };

    const arb = makeTableArbitrary(table, schema, {}, 3);
    fc.assert(
      fc.property(arb, rows => {
        return rows.every(r => !('id' in r) && typeof r['label'] === 'string');
      }),
    );
  });

  it('applies user overrides', () => {
    const table: TableInfo = {
      name: 'items',
      columns: [col('status', 'text')],
      primaryKey: [],
      foreignKeys: [],
      uniqueConstraints: [],
      checkConstraints: [],
    };
    const schema: SchemaInfo = { tables: [table], enums: [] };

    const arb = makeTableArbitrary(table, schema, {}, 4, {
      status: fc.constant('active'),
    });
    fc.assert(
      fc.property(arb, rows => rows.every(r => r['status'] === 'active')),
    );
  });
});

// ---------------------------------------------------------------------------
// FK with SERIAL parent PK (placeholder generation)
// ---------------------------------------------------------------------------

describe('makeTableArbitrary – FK with SERIAL parent PK', () => {
  const parentTable: TableInfo = {
    name: 'customers',
    columns: [
      col('id', 'serial', { isGenerated: true }),
      col('name', 'text'),
    ],
    primaryKey: ['id'],
    foreignKeys: [],
    uniqueConstraints: [],
    checkConstraints: [],
  };

  const childTable: TableInfo = {
    name: 'orders',
    columns: [
      col('id', 'serial', { isGenerated: true }),
      col('customer_id', 'integer'),
      col('total', 'numeric', { precision: 10, scale: 2 }),
    ],
    primaryKey: ['id'],
    foreignKeys: [
      { columns: ['customer_id'], referencedTable: 'customers', referencedColumns: ['id'] },
    ],
    uniqueConstraints: [],
    checkConstraints: [],
  };

  const schema: SchemaInfo = {
    tables: [parentTable, childTable],
    enums: [],
  };

  it('generates 1-based placeholder integers for FK referencing SERIAL PK', () => {
    // Simulate 3 parent rows generated without id (SERIAL, so isGenerated)
    const existingData: Dataset = {
      customers: [
        { name: 'Alice' },
        { name: 'Bob' },
        { name: 'Charlie' },
      ],
    };

    const arb = makeTableArbitrary(childTable, schema, existingData, 5);
    fc.assert(
      fc.property(arb, rows => {
        return rows.every(r => {
          const fkVal = r['customer_id'];
          // Should be one of the placeholder indices [1, 2, 3]
          return typeof fkVal === 'number' && fkVal >= 1 && fkVal <= 3;
        });
      }),
    );
  });

  it('placeholder indices cover all parent rows when generating many children', () => {
    const existingData: Dataset = {
      customers: [
        { name: 'Alice' },
        { name: 'Bob' },
      ],
    };

    const arb = makeTableArbitrary(childTable, schema, existingData, 20);
    const samples = fc.sample(arb, 10);
    const allFkValues = new Set(samples.flatMap(rows => rows.map(r => r['customer_id'])));
    // With 20 rows from 2 parents, both placeholders should appear
    expect(allFkValues.has(1)).toBe(true);
    expect(allFkValues.has(2)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// FK with non-SERIAL parent PK (standard value picking)
// ---------------------------------------------------------------------------

describe('makeTableArbitrary – FK with non-SERIAL parent PK', () => {
  const parentTable: TableInfo = {
    name: 'countries',
    columns: [col('code', 'varchar')],
    primaryKey: ['code'],
    foreignKeys: [],
    uniqueConstraints: [],
    checkConstraints: [],
  };

  const childTable: TableInfo = {
    name: 'cities',
    columns: [
      col('name', 'text'),
      col('country_code', 'varchar'),
    ],
    primaryKey: ['name'],
    foreignKeys: [
      { columns: ['country_code'], referencedTable: 'countries', referencedColumns: ['code'] },
    ],
    uniqueConstraints: [],
    checkConstraints: [],
  };

  const schema: SchemaInfo = {
    tables: [parentTable, childTable],
    enums: [],
  };

  it('picks actual parent values for FK referencing non-generated PK', () => {
    const existingData: Dataset = {
      countries: [{ code: 'US' }, { code: 'CA' }, { code: 'UK' }],
    };

    const arb = makeTableArbitrary(childTable, schema, existingData, 5);
    fc.assert(
      fc.property(arb, rows => {
        return rows.every(r => ['US', 'CA', 'UK'].includes(String(r['country_code'])));
      }),
    );
  });
});

// ---------------------------------------------------------------------------
// UNIQUE constraint handling
// ---------------------------------------------------------------------------

describe('makeTableArbitrary – UNIQUE constraints', () => {
  it('enforces single-column uniqueness', () => {
    const table: TableInfo = {
      name: 'users',
      columns: [
        col('email', 'text'),
        col('name', 'text'),
      ],
      primaryKey: ['email'],
      foreignKeys: [],
      uniqueConstraints: [['email']],
      checkConstraints: [],
    };
    const schema: SchemaInfo = { tables: [table], enums: [] };

    const arb = makeTableArbitrary(table, schema, {}, 5);
    fc.assert(
      fc.property(arb, rows => {
        const emails = rows.map(r => String(r['email']));
        return new Set(emails).size === emails.length;
      }),
    );
  });

  it('enforces multi-column uniqueness', () => {
    const table: TableInfo = {
      name: 'enrollments',
      columns: [
        col('student', 'text'),
        col('course', 'text'),
      ],
      primaryKey: [],
      foreignKeys: [],
      uniqueConstraints: [['student', 'course']],
      checkConstraints: [],
    };
    const schema: SchemaInfo = { tables: [table], enums: [] };

    const arb = makeTableArbitrary(table, schema, {}, 3);
    fc.assert(
      fc.property(arb, rows => {
        const keys = rows.map(r => `${r['student']}|${r['course']}`);
        return new Set(keys).size === keys.length;
      }),
    );
  });
});

// ---------------------------------------------------------------------------
// CHECK constraint integration
// ---------------------------------------------------------------------------

describe('makeTableArbitrary – CHECK constraints', () => {
  it('applies CHECK constraints to columns', () => {
    const table: TableInfo = {
      name: 'products',
      columns: [
        col('name', 'text'),
        col('price', 'numeric', { precision: 10, scale: 2 }),
      ],
      primaryKey: ['name'],
      foreignKeys: [],
      uniqueConstraints: [],
      checkConstraints: [
        { expression: 'price > 0', parsed: { column: 'price', operator: '>', value: 0 } },
      ],
    };
    const schema: SchemaInfo = { tables: [table], enums: [] };

    const arb = makeTableArbitrary(table, schema, {}, 5);
    fc.assert(
      fc.property(arb, rows => {
        return rows.every(r => Number(r['price']) > 0);
      }),
    );
  });
});

// ---------------------------------------------------------------------------
// Empty parent rows (self-referential FK edge case)
// ---------------------------------------------------------------------------

describe('makeTableArbitrary – self-referential FK', () => {
  it('produces null for FK when no parent rows exist yet', () => {
    const table: TableInfo = {
      name: 'categories',
      columns: [
        col('name', 'text'),
        col('parent_name', 'text', { nullable: true }),
      ],
      primaryKey: ['name'],
      foreignKeys: [
        { columns: ['parent_name'], referencedTable: 'categories', referencedColumns: ['name'] },
      ],
      uniqueConstraints: [],
      checkConstraints: [],
    };
    const schema: SchemaInfo = { tables: [table], enums: [] };

    // No existing data for categories yet (first pass)
    const arb = makeTableArbitrary(table, schema, {}, 3);
    fc.assert(
      fc.property(arb, rows => {
        return rows.every(r => r['parent_name'] === null);
      }),
    );
  });
});

// ---------------------------------------------------------------------------
// FK distribution strategy pass-through
// ---------------------------------------------------------------------------

describe('makeTableArbitrary – fkDistribution', () => {
  const parentTable: TableInfo = {
    name: 'customers',
    columns: [
      col('id', 'serial', { isGenerated: true }),
      col('name', 'text'),
    ],
    primaryKey: ['id'],
    foreignKeys: [],
    uniqueConstraints: [],
    checkConstraints: [],
  };

  const childTable: TableInfo = {
    name: 'orders',
    columns: [
      col('id', 'serial', { isGenerated: true }),
      col('customer_id', 'integer'),
    ],
    primaryKey: ['id'],
    foreignKeys: [
      { columns: ['customer_id'], referencedTable: 'customers', referencedColumns: ['id'] },
    ],
    uniqueConstraints: [],
    checkConstraints: [],
  };

  const schema: SchemaInfo = { tables: [parentTable, childTable], enums: [] };

  // 5 parent rows (no id since SERIAL)
  const existingData: Dataset = {
    customers: [
      { name: 'A' }, { name: 'B' }, { name: 'C' }, { name: 'D' }, { name: 'E' },
    ],
  };

  it('adversarial: customer_id only picks boundary placeholder values', () => {
    const arb = makeTableArbitrary(
      childTable, schema, existingData, 20,
      undefined,
      { customer_id: 'adversarial' },
    );
    fc.assert(
      fc.property(arb, rows => {
        // 5 parents → boundaries are placeholders 1, 3, 5
        return rows.every(r => [1, 3, 5].includes(Number(r['customer_id'])));
      }),
    );
  });

  it('zipf: customer_id still only picks valid placeholder values', () => {
    const arb = makeTableArbitrary(
      childTable, schema, existingData, 10,
      undefined,
      { customer_id: 'zipf' },
    );
    fc.assert(
      fc.property(arb, rows => {
        return rows.every(r => [1, 2, 3, 4, 5].includes(Number(r['customer_id'])));
      }),
    );
  });
});
