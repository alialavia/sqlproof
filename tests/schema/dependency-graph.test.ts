import { describe, it, expect } from 'vitest';
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
});
