import { describe, it, expect } from 'vitest';
import { parseSchemaFromSql } from '../../src/schema/parser.js';

describe('parseSchemaFromSql', () => {
  it('parses a simple CREATE TABLE', () => {
    const sql = `
      CREATE TABLE users (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        email TEXT
      );
    `;
    const schema = parseSchemaFromSql(sql);
    expect(schema.tables).toHaveLength(1);
    const table = schema.tables[0]!;
    expect(table.name).toBe('users');
    expect(table.columns).toHaveLength(3);
    expect(table.columns[0]!.name).toBe('id');
    expect(table.columns[0]!.isGenerated).toBe(true);
    expect(table.columns[1]!.nullable).toBe(false);
    expect(table.columns[2]!.nullable).toBe(true);
  });

  it('parses CREATE TYPE AS ENUM', () => {
    const sql = `
      CREATE TYPE status AS ENUM ('active', 'inactive', 'pending');
      CREATE TABLE items (id SERIAL PRIMARY KEY, status status NOT NULL);
    `;
    const schema = parseSchemaFromSql(sql);
    expect(schema.enums).toHaveLength(1);
    expect(schema.enums[0]!.name).toBe('status');
    expect(schema.enums[0]!.values).toEqual(['active', 'inactive', 'pending']);
  });

  it('parses inline FOREIGN KEY', () => {
    const sql = `
      CREATE TABLE orders (
        id SERIAL PRIMARY KEY,
        customer_id INTEGER NOT NULL REFERENCES customers(id)
      );
    `;
    const schema = parseSchemaFromSql(sql);
    const table = schema.tables[0]!;
    expect(table.foreignKeys).toHaveLength(1);
    expect(table.foreignKeys[0]!.columns).toEqual(['customer_id']);
    expect(table.foreignKeys[0]!.referencedTable).toBe('customers');
    expect(table.foreignKeys[0]!.referencedColumns).toEqual(['id']);
  });

  it('parses table-level FOREIGN KEY', () => {
    const sql = `
      CREATE TABLE line_items (
        id SERIAL PRIMARY KEY,
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (product_id) REFERENCES products(id)
      );
    `;
    const schema = parseSchemaFromSql(sql);
    const table = schema.tables[0]!;
    expect(table.foreignKeys).toHaveLength(2);
    expect(table.foreignKeys[0]!.referencedTable).toBe('orders');
    expect(table.foreignKeys[1]!.referencedTable).toBe('products');
  });

  it('parses simple CHECK constraints', () => {
    const sql = `
      CREATE TABLE products (
        id SERIAL PRIMARY KEY,
        price NUMERIC(10,2) NOT NULL CHECK (price > 0),
        stock INTEGER NOT NULL CHECK (stock >= 0),
        discount REAL CHECK (discount <= 1)
      );
    `;
    const schema = parseSchemaFromSql(sql);
    const table = schema.tables[0]!;
    const checks = table.checkConstraints.filter(c => c.parsed != null);
    expect(checks.length).toBeGreaterThanOrEqual(2);
    const priceCheck = checks.find(c => c.parsed?.column === 'price');
    expect(priceCheck?.parsed?.operator).toBe('>');
    expect(priceCheck?.parsed?.value).toBe(0);
  });

  it('parses IN CHECK constraint', () => {
    const sql = `
      CREATE TABLE items (
        id SERIAL PRIMARY KEY,
        status VARCHAR(20) CHECK (status IN ('active', 'inactive'))
      );
    `;
    const schema = parseSchemaFromSql(sql);
    const table = schema.tables[0]!;
    const check = table.checkConstraints.find(c => c.parsed?.operator === 'IN');
    expect(check).toBeDefined();
    expect(check?.parsed?.value).toEqual(['active', 'inactive']);
  });

  it('parses BETWEEN CHECK constraint', () => {
    const sql = `
      CREATE TABLE items (
        id SERIAL PRIMARY KEY,
        quantity INTEGER CHECK (quantity BETWEEN 1 AND 100)
      );
    `;
    const schema = parseSchemaFromSql(sql);
    const table = schema.tables[0]!;
    const check = table.checkConstraints.find(c => c.parsed?.operator === 'BETWEEN');
    expect(check).toBeDefined();
    expect(check?.parsed?.value).toBe(1);
    expect(check?.parsed?.value2).toBe(100);
  });

  it('parses multi-column PRIMARY KEY', () => {
    const sql = `
      CREATE TABLE order_products (
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        PRIMARY KEY (order_id, product_id)
      );
    `;
    const schema = parseSchemaFromSql(sql);
    const table = schema.tables[0]!;
    expect(table.primaryKey).toEqual(['order_id', 'product_id']);
  });

  it('parses table-level UNIQUE constraints', () => {
    const sql = `
      CREATE TABLE users (
        id SERIAL PRIMARY KEY,
        email TEXT NOT NULL,
        username TEXT NOT NULL,
        UNIQUE (email),
        UNIQUE (username)
      );
    `;
    const schema = parseSchemaFromSql(sql);
    const table = schema.tables[0]!;
    expect(table.uniqueConstraints.length).toBeGreaterThanOrEqual(2);
  });

  it('parses GENERATED ALWAYS AS IDENTITY', () => {
    const sql = `
      CREATE TABLE items (
        id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        name TEXT NOT NULL
      );
    `;
    const schema = parseSchemaFromSql(sql);
    const table = schema.tables[0]!;
    const idCol = table.columns.find(c => c.name === 'id');
    expect(idCol?.isGenerated).toBe(true);
  });

  it('parses the full e-commerce schema', () => {
    const sql = `
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
    `;
    const schema = parseSchemaFromSql(sql);
    expect(schema.tables).toHaveLength(4);
    expect(schema.enums).toHaveLength(1);
    expect(schema.enums[0]!.name).toBe('order_status');

    const ordersTable = schema.tables.find(t => t.name === 'orders')!;
    expect(ordersTable.foreignKeys).toHaveLength(1);
    expect(ordersTable.foreignKeys[0]!.referencedTable).toBe('customers');

    const lineItemsTable = schema.tables.find(t => t.name === 'line_items')!;
    expect(lineItemsTable.foreignKeys).toHaveLength(2);
  });

  it('strips SQL comments before parsing', () => {
    const sql = `
      -- This is a comment
      CREATE TABLE /* inline comment */ users (
        id SERIAL PRIMARY KEY, -- another comment
        name TEXT NOT NULL
      );
    `;
    const schema = parseSchemaFromSql(sql);
    expect(schema.tables).toHaveLength(1);
    expect(schema.tables[0]!.name).toBe('users');
  });

  it('handles named constraints', () => {
    const sql = `
      CREATE TABLE orders (
        id SERIAL PRIMARY KEY,
        amount NUMERIC,
        CONSTRAINT amount_positive CHECK (amount > 0)
      );
    `;
    const schema = parseSchemaFromSql(sql);
    const table = schema.tables[0]!;
    expect(table.checkConstraints).toHaveLength(1);
    expect(table.checkConstraints[0]!.parsed?.operator).toBe('>');
  });
});
