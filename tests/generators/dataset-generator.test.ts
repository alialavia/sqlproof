import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { makeDatasetArbitrary } from '../../src/generators/dataset-generator.js';
import type { SchemaInfo, TableCustomization } from '../../src/schema/types.js';

const schema: SchemaInfo = {
  tables: [
    {
      name: 'users',
      columns: [
        { name: 'id', dataType: 'serial', nullable: false, isGenerated: true, constraints: {}, isArray: false },
        { name: 'name', dataType: 'text', nullable: false, isGenerated: false, constraints: {}, isArray: false },
      ],
      primaryKey: ['id'],
      foreignKeys: [],
      uniqueConstraints: [],
      checkConstraints: [],
    },
    {
      name: 'posts',
      columns: [
        { name: 'id', dataType: 'serial', nullable: false, isGenerated: true, constraints: {}, isArray: false },
        { name: 'user_id', dataType: 'integer', nullable: false, isGenerated: false, constraints: {}, isArray: false },
        { name: 'title', dataType: 'text', nullable: false, isGenerated: false, constraints: {}, isArray: false },
      ],
      primaryKey: ['id'],
      foreignKeys: [{ columns: ['user_id'], referencedTable: 'users', referencedColumns: ['id'] }],
      uniqueConstraints: [],
      checkConstraints: [],
    },
  ],
  enums: [],
};

describe('makeDatasetArbitrary', () => {
  it('generates the specified number of rows per table', () => {
    const arb = makeDatasetArbitrary(schema, { users: 3, posts: 7 });
    fc.assert(
      fc.property(arb, dataset => {
        return dataset['users']!.length === 3 && dataset['posts']!.length === 7;
      }),
    );
  });

  it('only generates tables listed in rowCounts', () => {
    const arb = makeDatasetArbitrary(schema, { users: 2 });
    fc.assert(
      fc.property(arb, dataset => {
        return 'users' in dataset && !('posts' in dataset);
      }),
    );
  });

  it('applies column overrides from customizations', () => {
    const customizations = new Map<string, TableCustomization>([
      ['users', { name: fc.constant('Alice') }],
    ]);
    const arb = makeDatasetArbitrary(schema, { users: 3, posts: 5 }, customizations);
    fc.assert(
      fc.property(arb, dataset => {
        return dataset['users']!.every(r => r['name'] === 'Alice');
      }),
    );
  });

  it('generates zero-row tables when count is 0', () => {
    const arb = makeDatasetArbitrary(schema, { users: 0, posts: 0 });
    fc.assert(
      fc.property(arb, dataset => {
        return dataset['users']!.length === 0 && dataset['posts']!.length === 0;
      }),
    );
  });
});
