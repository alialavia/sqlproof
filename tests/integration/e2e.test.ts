import { describe, it } from 'vitest';
import { sqlproof } from '../../src/index.js';

const schema = new URL('../../examples/orders/schema.sql', import.meta.url).pathname;

describe('e2e integration tests', { timeout: 60000 }, () => {
  it('passes a trivially-true property', async () => {
    await sqlproof.check({
      name: 'trivially true property',
      schema,
      runs: 10,
      rowsPerTable: 3,
      property: async () => true,
    });
  });

  it('every line item references a valid order (FK integrity)', async () => {
    await sqlproof.check({
      name: 'line items have valid order references',
      schema,
      runs: 10,
      rowsPerTable: 3,
      property: async db => {
        const result = await db.query(`
          SELECT li.id
          FROM line_items li
          LEFT JOIN orders o ON li.order_id = o.id
          WHERE o.id IS NULL
        `);
        return result.rows.length === 0;
      },
    });
  });

  it('order totals are non-negative (CHECK constraint respected)', async () => {
    await sqlproof.check({
      name: 'order totals are non-negative',
      schema,
      runs: 10,
      rowsPerTable: 3,
      property: async db => {
        const result = await db.query('SELECT total FROM orders');
        return result.rows.every(row => Number(row['total']) >= 0);
      },
    });
  });
});
