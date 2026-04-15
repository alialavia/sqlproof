import * as fc from 'fast-check';
import type { SchemaInfo, Dataset, TableCustomization, FkDistributionStrategy } from '../schema/types.js';
import { getInsertionOrder } from '../schema/dependency-graph.js';
import { makeTableArbitrary } from './table-generator.js';

/**
 * Builds a fast-check Arbitrary that generates a complete multi-table dataset.
 * Tables are generated in FK dependency order.
 *
 * rowCounts: per-table row count map, e.g. { customers: 20, orders: 100 }.
 *   Only tables whose names appear as keys are generated.
 * customizations: per-table column overrides and FK distribution strategies.
 */
export function makeDatasetArbitrary(
  schema: SchemaInfo,
  rowCounts: Record<string, number>,
  customizations?: Map<string, TableCustomization>,
): fc.Arbitrary<Dataset> {
  const orderedTables = getInsertionOrder(schema.tables).filter(name =>
    Object.prototype.hasOwnProperty.call(rowCounts, name),
  );

  return fc.gen().map(gen => {
    const dataset: Dataset = {};

    for (const tableName of orderedTables) {
      const table = schema.tables.find(t => t.name === tableName);
      if (!table) continue;

      const rowCount = rowCounts[tableName] ?? 0;
      const customization = customizations?.get(tableName);

      // Split customization into column overrides and FK distribution
      const { fkDistribution, ...columnOverrides } = customization ?? {};
      const colOverrides = columnOverrides as Record<string, fc.Arbitrary<unknown>>;
      const fkDist = fkDistribution as Record<string, FkDistributionStrategy> | undefined;

      const tableArb = makeTableArbitrary(
        table,
        schema,
        dataset,
        rowCount,
        Object.keys(colOverrides).length > 0 ? colOverrides : undefined,
        fkDist,
      );

      dataset[tableName] = gen(() => tableArb);
    }

    return dataset;
  });
}
