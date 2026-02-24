import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { getArbitraryForColumn } from '../../src/generators/column-generators.js';
import type { ColumnInfo } from '../../src/schema/types.js';

function makeCol(overrides: Partial<ColumnInfo> & { dataType: string }): ColumnInfo {
  return {
    name: 'col',
    nullable: false,
    isGenerated: false,
    constraints: {},
    isArray: false,
    ...overrides,
  };
}

describe('getArbitraryForColumn', () => {
  it('generates integers in valid range', () => {
    fc.assert(
      fc.property(getArbitraryForColumn(makeCol({ dataType: 'integer' }), []), v => {
        const n = Number(v);
        return n >= -2147483648 && n <= 2147483647;
      }),
    );
  });

  it('generates smallints in valid range', () => {
    fc.assert(
      fc.property(getArbitraryForColumn(makeCol({ dataType: 'smallint' }), []), v => {
        const n = Number(v);
        return n >= -32768 && n <= 32767;
      }),
    );
  });

  it('generates booleans', () => {
    fc.assert(
      fc.property(getArbitraryForColumn(makeCol({ dataType: 'boolean' }), []), v => {
        return typeof v === 'boolean';
      }),
    );
  });

  it('generates valid UUIDs', () => {
    fc.assert(
      fc.property(getArbitraryForColumn(makeCol({ dataType: 'uuid' }), []), v => {
        return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(
          String(v),
        );
      }),
    );
  });

  it('generates valid ISO dates', () => {
    fc.assert(
      fc.property(getArbitraryForColumn(makeCol({ dataType: 'date' }), []), v => {
        return /^\d{4}-\d{2}-\d{2}$/.test(String(v));
      }),
    );
  });

  it('generates valid time strings', () => {
    fc.assert(
      fc.property(getArbitraryForColumn(makeCol({ dataType: 'time' }), []), v => {
        return /^\d{2}:\d{2}:\d{2}$/.test(String(v));
      }),
    );
  });

  it('generates enum values', () => {
    const enums = [{ name: 'status', values: ['active', 'inactive', 'pending'] }];
    fc.assert(
      fc.property(getArbitraryForColumn(makeCol({ dataType: 'status' }), enums), v => {
        return ['active', 'inactive', 'pending'].includes(String(v));
      }),
    );
  });

  it('generates varchar within length constraint', () => {
    const col = makeCol({ dataType: 'varchar', constraints: { length: 10 } });
    const segmenter = new Intl.Segmenter();
    fc.assert(
      fc.property(getArbitraryForColumn(col, []), v => {
        // Count grapheme clusters (matches fc.string unit: 'grapheme')
        return [...segmenter.segment(String(v))].length <= 10;
      }),
    );
  });

  it('returns null sentinel for generated columns', () => {
    const col = makeCol({ dataType: 'serial', isGenerated: true });
    fc.assert(
      fc.property(getArbitraryForColumn(col, []), v => {
        return v === null;
      }),
    );
  });

  it('generates floats without NaN', () => {
    fc.assert(
      fc.property(getArbitraryForColumn(makeCol({ dataType: 'real' }), []), v => {
        return !isNaN(Number(v)) && isFinite(Number(v));
      }),
    );
  });

  it('generates valid timestamps', () => {
    fc.assert(
      fc.property(getArbitraryForColumn(makeCol({ dataType: 'timestamp' }), []), v => {
        return /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(String(v));
      }),
    );
  });

  it('generates JSON strings', () => {
    fc.assert(
      fc.property(getArbitraryForColumn(makeCol({ dataType: 'jsonb' }), []), v => {
        try {
          JSON.parse(String(v));
          return true;
        } catch {
          return false;
        }
      }),
    );
  });

  it('generates bigint as string', () => {
    fc.assert(
      fc.property(getArbitraryForColumn(makeCol({ dataType: 'bigint' }), []), v => {
        return typeof v === 'string' && !isNaN(Number(v));
      }),
    );
  });
});
