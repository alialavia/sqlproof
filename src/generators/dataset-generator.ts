import * as fc from 'fast-check';
import type { SchemaInfo, Dataset, SqlProofCheckOptions } from '../schema/types.js';
import { getInsertionOrder } from '../schema/dependency-graph.js';
import { makeTableArbitrary } from './table-generator.js';

/**
 * Builds a fast-check Arbitrary that generates a complete multi-table dataset.
 * Tables are generated in FK dependency order so that FK columns can reference
 * already-generated parent rows.
 *
 * Uses fc.gen() so that fast-check can shrink each table's rows independently
 * while still passing previously-generated data to child table generators.
 */
export function makeDatasetArbitrary(
  schema: SchemaInfo,
  rowsPerTable: number,
  overrides?: SqlProofCheckOptions['overrides'],
  tableFilter?: string[],
): fc.Arbitrary<Dataset> {
  const orderedTables = getInsertionOrder(schema.tables).filter(name =>
    tableFilter == null || tableFilter.includes(name),
  );

  return fc.gen().map(gen => {
    const dataset: Dataset = {};

    for (const tableName of orderedTables) {
      const table = schema.tables.find(t => t.name === tableName);
      if (!table) continue;

      const tableOverrides = overrides?.[tableName];
      const tableArb = makeTableArbitrary(
        table,
        schema,
        dataset,
        rowsPerTable,
        tableOverrides,
      );

      dataset[tableName] = gen(() => tableArb);
    }

    return dataset;
  });
}
