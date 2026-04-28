import { describe, it, beforeEach, afterEach } from 'vitest';
import { SqlProof } from '../../src/index.js';
import { getTestDatabaseUrl } from './test-database.js';

const schemaFile = new URL('../../examples/orders/schema.sql', import.meta.url).pathname;
const connectionString = getTestDatabaseUrl();

describe('e2e integration tests', { timeout: 120000 }, () => {
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

  it('passes a trivially-true property', async () => {
    await proof.check('trivially true property', {
      generate: { customers: 3, orders: 3, products: 3, line_items: 3 },
      property: async () => true,
      runs: 10,
    });
  });

  it('recreates enum defaults from schema files inside the run schema', async () => {
    await proof.check('schema file enum defaults are valid in run schemas', {
      generate: { customers: 0, orders: 0, products: 0, line_items: 0 },
      property: async db => {
        await db.query('SELECT 1');
        return true;
      },
      runs: 1,
    });
  });

  it('every line item references a valid order (FK integrity)', async () => {
    await proof.check('line items have valid order references', {
      generate: { customers: 3, orders: 3, products: 3, line_items: 3 },
      property: async db => {
        const result = await db.query(`
          SELECT li.id
          FROM line_items li
          LEFT JOIN orders o ON li.order_id = o.id
          WHERE o.id IS NULL
        `);
        return result.rows.length === 0;
      },
      runs: 10,
    });
  });

  it('order totals are non-negative (CHECK constraint respected)', async () => {
    await proof.check('order totals are non-negative', {
      generate: { customers: 3, orders: 3, products: 3, line_items: 3 },
      property: async db => {
        const result = await db.query('SELECT total FROM orders');
        return result.rows.every(row => Number(row['total']) >= 0);
      },
      runs: 10,
    });
  });

  it('invariant: no line items with null order reference', async () => {
    await proof.invariant('line items always have an order', {
      generate: { customers: 3, orders: 3, products: 3, line_items: 5 },
      query: `
        SELECT li.id FROM line_items li
        LEFT JOIN orders o ON li.order_id = o.id
        WHERE o.id IS NULL
      `,
      expectEmpty: true,
      runs: 10,
    });
  });

  it('zipf distribution: FK integrity still holds with skewed assignment', async () => {
    proof.customize('orders', { fkDistribution: { customer_id: 'zipf' } });
    proof.customize('line_items', {
      fkDistribution: { order_id: 'zipf', product_id: 'adversarial' },
    });

    await proof.check('FK integrity with custom distributions', {
      generate: { customers: 3, orders: 5, products: 3, line_items: 5 },
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
      runs: 10,
    });
  });
});
