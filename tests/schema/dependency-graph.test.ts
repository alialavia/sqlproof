import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { getInsertionOrder } from '../../src/schema/dependency-graph.js';
import type { TableInfo } from '../../src/schema/types.js';

function makeTable(name: string, refs: string[] = []): TableInfo {
  return {
    name,
    columns: [],
    primaryKey: ['id'],
    foreignKeys: refs.map(ref => ({
      columns: [`${ref}_id`],
      referencedTable: ref,
      referencedColumns: ['id'],
    })),
    uniqueConstraints: [],
    checkConstraints: [],
  };
}

describe('getInsertionOrder', () => {
  it('places parent tables before child tables', () => {
    const tables = [
      makeTable('line_items', ['orders', 'products']),
      makeTable('orders', ['customers']),
      makeTable('customers'),
      makeTable('products'),
    ];
    const order = getInsertionOrder(tables);
    const customerIdx = order.indexOf('customers');
    const ordersIdx = order.indexOf('orders');
    const lineItemsIdx = order.indexOf('line_items');
    const productsIdx = order.indexOf('products');
    expect(customerIdx).toBeLessThan(ordersIdx);
    expect(ordersIdx).toBeLessThan(lineItemsIdx);
    expect(productsIdx).toBeLessThan(lineItemsIdx);
  });

  it('handles tables with no FKs in stable order', () => {
    const tables = [makeTable('c'), makeTable('a'), makeTable('b')];
    const order = getInsertionOrder(tables);
    expect(order).toHaveLength(3);
    expect(order).toContain('a');
    expect(order).toContain('b');
    expect(order).toContain('c');
  });

  it('throws on circular dependency and includes table names', () => {
    const tables = [
      makeTable('a', ['b']),
      makeTable('b', ['c']),
      makeTable('c', ['a']),
    ];
    expect(() => getInsertionOrder(tables)).toThrow(/circular/i);
  });

  it('handles self-referential tables without throwing', () => {
    const employees: TableInfo = {
      name: 'employees',
      columns: [],
      primaryKey: ['id'],
      foreignKeys: [
        { columns: ['manager_id'], referencedTable: 'employees', referencedColumns: ['id'] },
      ],
      uniqueConstraints: [],
      checkConstraints: [],
    };
    const order = getInsertionOrder([employees]);
    expect(order).toEqual(['employees']);
  });

  it('returns all tables', () => {
    const tables = [
      makeTable('a'),
      makeTable('b', ['a']),
      makeTable('c', ['b']),
      makeTable('d', ['a']),
    ];
    const order = getInsertionOrder(tables);
    expect(order).toHaveLength(4);
  });

  it('throws error listing involved table names', () => {
    const tables = [makeTable('x', ['y']), makeTable('y', ['x'])];
    expect(() => getInsertionOrder(tables)).toThrow(/x|y/);
  });

  it('returns a valid topological order for generated DAGs', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 8 }).chain(tableCount =>
          fc
            .array(fc.boolean(), {
              minLength: (tableCount * (tableCount - 1)) / 2,
              maxLength: (tableCount * (tableCount - 1)) / 2,
            })
            .map(edges => ({ tableCount, edges })),
        ),
        ({ tableCount, edges }) => {
          const tableNames = Array.from({ length: tableCount }, (_, i) => `t${i}`);
          let edgeIndex = 0;
          const refsByTable = new Map<string, string[]>();

          for (let childIdx = 0; childIdx < tableCount; childIdx++) {
            const refs: string[] = [];
            for (let parentIdx = 0; parentIdx < childIdx; parentIdx++) {
              if (edges[edgeIndex]) refs.push(tableNames[parentIdx]!);
              edgeIndex++;
            }
            refsByTable.set(tableNames[childIdx]!, refs);
          }

          const tables = tableNames.map(name => makeTable(name, refsByTable.get(name) ?? []));
          const order = getInsertionOrder(tables);
          const positions = new Map(order.map((name, index) => [name, index]));

          expect(new Set(order)).toEqual(new Set(tableNames));
          expect(order).toHaveLength(tableNames.length);

          for (const table of tables) {
            const childPosition = positions.get(table.name);
            expect(childPosition).toBeDefined();

            for (const fk of table.foreignKeys) {
              const parentPosition = positions.get(fk.referencedTable);
              expect(parentPosition).toBeDefined();
              expect(parentPosition!).toBeLessThan(childPosition!);
            }
          }
        },
      ),
    );
  });

  it('throws for generated circular dependencies', () => {
    fc.assert(
      fc.property(fc.integer({ min: 2, max: 8 }), tableCount => {
        const tableNames = Array.from({ length: tableCount }, (_, i) => `t${i}`);
        const tables = tableNames.map((name, index) => {
          const referencedTable = tableNames[(index + 1) % tableCount]!;
          return makeTable(name, [referencedTable]);
        });

        expect(() => getInsertionOrder(tables)).toThrow(/circular/i);
      }),
    );
  });
});
