import * as fc from 'fast-check';
import type { TableInfo, SchemaInfo, Dataset } from '../schema/types.js';
import { getArbitraryForColumn } from './column-generators.js';
import { applyNullability, applyCheckConstraint, makeForeignKeyArbitrary } from './constraint-handler.js';

/**
 * Builds a fast-check Arbitrary that generates `rowCount` rows for `table`.
 * Parent table rows (for FK resolution) must already be in `existingData`.
 */
export function makeTableArbitrary(
  table: TableInfo,
  schema: SchemaInfo,
  existingData: Dataset,
  rowCount: number,
  overrides?: Record<string, fc.Arbitrary<unknown>>,
): fc.Arbitrary<Record<string, unknown>[]> {
  // Build per-column arbitraries
  const colArbitraries: Record<string, fc.Arbitrary<unknown>> = {};
  const fkColumnSet = new Set<string>();

  // Index FK info: column name -> {referencedTable, referencedColumn}
  for (const fk of table.foreignKeys) {
    for (let i = 0; i < fk.columns.length; i++) {
      fkColumnSet.add(fk.columns[i]!);
    }
  }

  for (const col of table.columns) {
    if (col.isGenerated) continue; // skip — DB assigns these

    // User overrides take highest priority
    if (overrides?.[col.name]) {
      colArbitraries[col.name] = overrides[col.name]!;
      continue;
    }

    // FK column — pick from already-generated parent rows
    if (fkColumnSet.has(col.name)) {
      const fk = table.foreignKeys.find(fk => fk.columns.includes(col.name));
      if (fk) {
        const colIdx = fk.columns.indexOf(col.name);
        const referencedCol = fk.referencedColumns[colIdx] ?? 'id';
        const parentTable = fk.referencedTable;

        // For self-referential FKs, parentRows may be empty on first pass
        const parentRows = existingData[parentTable] ?? [];
        let fkArb = makeForeignKeyArbitrary(parentRows, referencedCol);
        fkArb = applyNullability(fkArb, col);
        colArbitraries[col.name] = fkArb;
        continue;
      }
    }

    // Base arbitrary for this column's type
    let arb = getArbitraryForColumn(col, schema.enums);

    // Apply CHECK constraints that reference only this column
    for (const check of table.checkConstraints) {
      if (check.parsed && check.parsed.column === col.name) {
        arb = applyCheckConstraint(arb, check.parsed, col);
      }
    }

    // Apply nullability
    arb = applyNullability(arb, col);

    colArbitraries[col.name] = arb;
  }

  // Build a single-row arbitrary
  const rowArb = fc.record(colArbitraries) as fc.Arbitrary<Record<string, unknown>>;

  // Handle UNIQUE constraints for single-column uniques
  const singleColUniques = table.uniqueConstraints.filter(cols => cols.length === 1);
  const multiColUniques = table.uniqueConstraints.filter(cols => cols.length > 1);

  let rowsArb: fc.Arbitrary<Record<string, unknown>[]>;

  if (singleColUniques.length > 0) {
    // Use the first single-column unique with fc.uniqueArray
    // For multiple single-column uniques, chain them
    const uniqueCol = singleColUniques[0]![0]!;
    rowsArb = fc.uniqueArray(rowArb, {
      minLength: rowCount,
      maxLength: rowCount,
      selector: row => String(row[uniqueCol]),
    });
  } else {
    rowsArb = fc.array(rowArb, { minLength: rowCount, maxLength: rowCount });
  }

  // Post-filter multi-column unique constraints
  if (multiColUniques.length > 0) {
    rowsArb = rowsArb.filter(rows => {
      for (const cols of multiColUniques) {
        const seen = new Set<string>();
        for (const row of rows) {
          const key = cols.map(c => String(row[c])).join('|');
          if (seen.has(key)) return false;
          seen.add(key);
        }
      }
      return true;
    });
  }

  return rowsArb;
}
