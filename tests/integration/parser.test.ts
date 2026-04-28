import { describe, it, expect, beforeAll, afterAll } from 'vitest';
import { Pool } from 'pg';
import { executeAndIntrospect } from '../../src/schema/parser.js';
import { getTestDatabaseUrl } from './test-database.js';

const connectionString = getTestDatabaseUrl();

describe('executeAndIntrospect', { timeout: 120000 }, () => {
  let pool: Pool;

  beforeAll(async () => {
    pool = new Pool({ connectionString });
  }, 120000);

  afterAll(async () => {
    await pool?.end();
  });

  it('parses a simple CREATE TABLE', async () => {
    const schema = await executeAndIntrospect(pool, `
      CREATE TABLE users (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        email TEXT
      );
    `);
    expect(schema.tables).toHaveLength(1);
    const table = schema.tables[0]!;
    expect(table.name).toBe('users');
    expect(table.columns).toHaveLength(3);
    expect(table.columns.find(c => c.name === 'id')!.isGenerated).toBe(true);
    expect(table.columns.find(c => c.name === 'name')!.nullable).toBe(false);
    expect(table.columns.find(c => c.name === 'email')!.nullable).toBe(true);
  });

  it('parses CREATE TYPE AS ENUM', async () => {
    const schema = await executeAndIntrospect(pool, `
      CREATE TYPE status AS ENUM ('active', 'inactive', 'pending');
      CREATE TABLE items (id SERIAL PRIMARY KEY, status status NOT NULL);
    `);
    expect(schema.enums).toHaveLength(1);
    expect(schema.enums[0]!.name).toBe('status');
    expect(schema.enums[0]!.values).toEqual(['active', 'inactive', 'pending']);
  });

  it('parses foreign keys', async () => {
    const schema = await executeAndIntrospect(pool, `
      CREATE TABLE customers (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL
      );
      CREATE TABLE orders (
        id SERIAL PRIMARY KEY,
        customer_id INTEGER NOT NULL REFERENCES customers(id)
      );
    `);
    const orders = schema.tables.find(t => t.name === 'orders')!;
    expect(orders.foreignKeys).toHaveLength(1);
    expect(orders.foreignKeys[0]!.columns).toEqual(['customer_id']);
    expect(orders.foreignKeys[0]!.referencedTable).toBe('customers');
    expect(orders.foreignKeys[0]!.referencedColumns).toEqual(['id']);
  });

  it('parses CHECK constraints', async () => {
    const schema = await executeAndIntrospect(pool, `
      CREATE TABLE products (
        id SERIAL PRIMARY KEY,
        price NUMERIC(10,2) NOT NULL CHECK (price > 0),
        stock INTEGER NOT NULL CHECK (stock >= 0)
      );
    `);
    const table = schema.tables[0]!;
    expect(table.checkConstraints.length).toBeGreaterThanOrEqual(2);
    expect(table.checkConstraints.every(c => c.expression.length > 0)).toBe(true);
  });

  it('parses multi-column PRIMARY KEY', async () => {
    const schema = await executeAndIntrospect(pool, `
      CREATE TABLE order_products (
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        PRIMARY KEY (order_id, product_id)
      );
    `);
    const table = schema.tables[0]!;
    expect(table.primaryKey).toEqual(expect.arrayContaining(['order_id', 'product_id']));
    expect(table.primaryKey).toHaveLength(2);
  });

  it('parses UNIQUE constraints', async () => {
    const schema = await executeAndIntrospect(pool, `
      CREATE TABLE users (
        id SERIAL PRIMARY KEY,
        email TEXT NOT NULL,
        username TEXT NOT NULL,
        UNIQUE (email),
        UNIQUE (username)
      );
    `);
    const table = schema.tables[0]!;
    expect(table.uniqueConstraints.length).toBeGreaterThanOrEqual(2);
  });

  it('parses GENERATED ALWAYS AS IDENTITY', async () => {
    const schema = await executeAndIntrospect(pool, `
      CREATE TABLE items (
        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name TEXT NOT NULL
      );
    `);
    const table = schema.tables[0]!;
    const idCol = table.columns.find(c => c.name === 'id');
    expect(idCol?.isGenerated).toBe(true);
  });

  it('parses the full e-commerce schema', async () => {
    const schema = await executeAndIntrospect(pool, `
      CREATE TYPE order_status AS ENUM ('pending', 'confirmed', 'shipped', 'delivered', 'cancelled');

      CREATE TABLE customers (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        email VARCHAR(255) NOT NULL UNIQUE,
        created_at TIMESTAMP NOT NULL DEFAULT NOW()
      );

      CREATE TABLE orders (
        id SERIAL PRIMARY KEY,
        customer_id INTEGER NOT NULL REFERENCES customers(id),
        status order_status NOT NULL DEFAULT 'pending',
        total NUMERIC(10,2) NOT NULL CHECK (total >= 0),
        created_at TIMESTAMP NOT NULL DEFAULT NOW()
      );

      CREATE TABLE products (
        id SERIAL PRIMARY KEY,
        name VARCHAR(200) NOT NULL,
        price NUMERIC(10,2) NOT NULL CHECK (price > 0),
        stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0)
      );

      CREATE TABLE line_items (
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL REFERENCES orders(id),
        product_id INTEGER NOT NULL REFERENCES products(id),
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        price NUMERIC(10,2) NOT NULL CHECK (price > 0)
      );
    `);
    expect(schema.tables).toHaveLength(4);
    expect(schema.enums).toHaveLength(1);
    expect(schema.enums[0]!.name).toBe('order_status');

    const ordersTable = schema.tables.find(t => t.name === 'orders')!;
    expect(ordersTable.foreignKeys).toHaveLength(1);
    expect(ordersTable.foreignKeys[0]!.referencedTable).toBe('customers');

    const lineItemsTable = schema.tables.find(t => t.name === 'line_items')!;
    expect(lineItemsTable.foreignKeys).toHaveLength(2);
  });

  it('handles SQL comments in DDL', async () => {
    const schema = await executeAndIntrospect(pool, `
      -- This is a comment
      CREATE TABLE /* inline comment */ users (
        id SERIAL PRIMARY KEY, -- another comment
        name TEXT NOT NULL
      );
    `);
    expect(schema.tables).toHaveLength(1);
    expect(schema.tables[0]!.name).toBe('users');
  });

  it('temp schema is cleaned up after introspection', async () => {
    await executeAndIntrospect(pool, `
      CREATE TABLE temp_test (id SERIAL PRIMARY KEY);
    `);
    const result = await pool.query(`
      SELECT schema_name FROM information_schema.schemata
      WHERE schema_name LIKE '_sqlproof_introspect_%'
    `);
    expect(result.rows).toHaveLength(0);
  });
});
