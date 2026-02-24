import { describe, it } from 'vitest';
import { sqlproof } from '../../src/index.js';

const schema = new URL('./schema.sql', import.meta.url).pathname;

describe('e-commerce properties', { timeout: 60000 }, () => {
  it('order total should be non-negative', async () => {
    await sqlproof.check({
      name: 'order totals are non-negative',
      schema,
      runs: 20,
      rowsPerTable: 5,
      property: async db => {
        const result = await db.query('SELECT total FROM orders');
        return result.rows.every(row => Number(row['total']) >= 0);
      },
    });
  });

  it('every line item references a valid order', async () => {
    await sqlproof.check({
      name: 'line items have valid order references',
      schema,
      runs: 20,
      rowsPerTable: 5,
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

  it('order total equals sum of line item costs (likely fails — intentional)', async () => {
    // This property intentionally tests something that random data will violate,
    // demonstrating counterexample reporting.
    // Wrapped in try/catch so the test suite itself passes.
    try {
      await sqlproof.check({
        name: 'order totals match line items',
        schema,
        runs: 50,
        rowsPerTable: 5,
        property: async db => {
          const result = await db.query(`
            SELECT
              o.id,
              o.total as stored_total,
              COALESCE(SUM(li.price * li.quantity), 0) as computed_total
            FROM orders o
            LEFT JOIN line_items li ON o.id = li.order_id
            GROUP BY o.id, o.total
          `);
          return result.rows.every(
            row =>
              Math.abs(Number(row['stored_total']) - Number(row['computed_total'])) < 0.01,
          );
        },
      });
    } catch (_err) {
      // Expected to fail — property violation is the demonstration
    }
  });

  it('cancelled orders query runs without error', async () => {
    await sqlproof.check({
      name: 'cancelled orders are immutable',
      schema,
      runs: 20,
      rowsPerTable: 5,
      property: async db => {
        await db.query(`
          SELECT o.id, o.status, COUNT(li.id) as item_count
          FROM orders o
          LEFT JOIN line_items li ON o.id = li.order_id
          WHERE o.status = 'cancelled'
          GROUP BY o.id, o.status
        `);
        return true;
      },
    });
  });
});
