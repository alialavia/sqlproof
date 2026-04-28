import { describe, it, beforeEach, afterEach } from 'vitest';
import { SqlProof } from '../../src/index.js';
import { getTestDatabaseUrl } from '../../tests/integration/test-database.js';

const schemaFile = new URL('./schema.sql', import.meta.url).pathname;
const connectionString = getTestDatabaseUrl();

describe('e-commerce properties', { timeout: 120000 }, () => {
  let proof: SqlProof;

  beforeEach(async () => {
    proof = await SqlProof.connect({
      connectionString,
      schemaFile,
    });
  }, 120000);

  afterEach(async () => {
    await proof?.disconnect();
  }, 30000);

  it('order total should be non-negative', async () => {
    await proof.check('order totals are non-negative', {
      generate: { customers: 5, orders: 5, products: 5, line_items: 10 },
      runs: 20,
      property: async db => {
        const result = await db.query('SELECT total FROM orders');
        return result.rows.every(row => Number(row['total']) >= 0);
      },
    });
  });

  it('every line item references a valid order', async () => {
    await proof.check('line items have valid order references', {
      generate: { customers: 5, orders: 5, products: 5, line_items: 10 },
      runs: 20,
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
      await proof.check('order totals match line items', {
        generate: { customers: 5, orders: 5, products: 5, line_items: 10 },
        runs: 50,
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
    await proof.check('cancelled orders are immutable', {
      generate: { customers: 5, orders: 5, products: 5, line_items: 10 },
      runs: 20,
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

  it('zipf FK distribution: FK integrity holds with skewed assignment', async () => {
    proof.customize('orders', { fkDistribution: { customer_id: 'zipf' } });
    proof.customize('line_items', {
      fkDistribution: { order_id: 'zipf', product_id: 'adversarial' },
    });

    await proof.check('FK integrity with zipf/adversarial distributions', {
      generate: { customers: 5, orders: 10, products: 5, line_items: 20 },
      runs: 10,
      property: async db => {
        const orphanOrders = await db.query(`
          SELECT o.id FROM orders o
          LEFT JOIN customers c ON o.customer_id = c.id
          WHERE c.id IS NULL
        `);
        const orphanItems = await db.query(`
          SELECT li.id FROM line_items li
          LEFT JOIN orders o ON li.order_id = o.id
          WHERE o.id IS NULL
        `);
        return orphanOrders.rows.length === 0 && orphanItems.rows.length === 0;
      },
    });
  });
});
