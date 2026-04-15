import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import {
  applyNullability,
  applyCheckConstraint,
  makeForeignKeyArbitrary,
  getNumericBounds,
  makeNumericArb,
  isIntegerType,
  isFloatType,
} from '../../src/generators/constraint-handler.js';
import type { ColumnInfo, ParsedCheck } from '../../src/schema/types.js';

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

// ---------------------------------------------------------------------------
// applyNullability
// ---------------------------------------------------------------------------

describe('applyNullability', () => {
  it('returns the original arbitrary for non-nullable columns', () => {
    const col = makeCol({ dataType: 'integer', nullable: false });
    const arb = fc.integer();
    const result = applyNullability(arb, col);
    fc.assert(fc.property(result, v => v !== null));
  });

  it('sometimes produces null for nullable columns', () => {
    const col = makeCol({ dataType: 'integer', nullable: true });
    const arb = fc.integer({ min: 1, max: 100 });
    const result = applyNullability(arb, col);
    const samples = fc.sample(result, 200);
    expect(samples.some(v => v === null)).toBe(true);
    expect(samples.some(v => v !== null)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// getNumericBounds
// ---------------------------------------------------------------------------

describe('getNumericBounds', () => {
  it('computes correct bounds for NUMERIC(10,2)', () => {
    const col = makeCol({ dataType: 'numeric', constraints: { precision: 10, scale: 2 } });
    const { numericMax, scale } = getNumericBounds(col);
    // 10 total digits, 2 after decimal → 8 integer digits → max 99_999_999
    expect(numericMax).toBe(99_999_999);
    expect(scale).toBe(2);
  });

  it('computes correct bounds for NUMERIC(5,3)', () => {
    const col = makeCol({ dataType: 'numeric', constraints: { precision: 5, scale: 3 } });
    const { numericMax, scale } = getNumericBounds(col);
    // 5 total digits, 3 after decimal → 2 integer digits → max 99
    expect(numericMax).toBe(99);
    expect(scale).toBe(3);
  });

  it('computes correct bounds for DECIMAL(3,0)', () => {
    const col = makeCol({ dataType: 'decimal', constraints: { precision: 3, scale: 0 } });
    const { numericMax, scale } = getNumericBounds(col);
    expect(numericMax).toBe(999);
    expect(scale).toBe(0);
  });

  it('uses sensible defaults for bare NUMERIC with no precision/scale', () => {
    const col = makeCol({ dataType: 'numeric', constraints: {} });
    const { numericMax, scale } = getNumericBounds(col);
    // defaults: precision=10, scale=2 → max 99_999_999
    expect(numericMax).toBe(99_999_999);
    expect(scale).toBe(2);
  });

  it('returns MAX_SAFE_INTEGER for non-numeric types', () => {
    const col = makeCol({ dataType: 'real', constraints: {} });
    const { numericMax, scale } = getNumericBounds(col);
    expect(numericMax).toBe(Number.MAX_SAFE_INTEGER);
    expect(scale).toBeUndefined();
  });

  it('returns MAX_SAFE_INTEGER for integer types', () => {
    const col = makeCol({ dataType: 'integer', constraints: {} });
    const { numericMax, scale } = getNumericBounds(col);
    expect(numericMax).toBe(Number.MAX_SAFE_INTEGER);
    expect(scale).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// makeNumericArb
// ---------------------------------------------------------------------------

describe('makeNumericArb', () => {
  it('generates values within [min, max]', () => {
    const arb = makeNumericArb(1, 100, undefined) as fc.Arbitrary<number>;
    fc.assert(fc.property(arb, v => v >= 1 && v <= 100));
  });

  it('rounds values to the given scale', () => {
    const arb = makeNumericArb(0, 1000, 2) as fc.Arbitrary<number>;
    fc.assert(
      fc.property(arb, v => {
        const str = String(v);
        const parts = str.split('.');
        // Either no decimal part or at most 2 digits after the decimal
        return parts.length === 1 || parts[1]!.length <= 2;
      }),
    );
  });

  it('produces no NaN or Infinity', () => {
    const arb = makeNumericArb(-1000, 1000, 3) as fc.Arbitrary<number>;
    fc.assert(fc.property(arb, v => !isNaN(v) && isFinite(v)));
  });
});

// ---------------------------------------------------------------------------
// applyCheckConstraint
// ---------------------------------------------------------------------------

describe('applyCheckConstraint', () => {
  describe('CHECK (col > N) on NUMERIC(10,2)', () => {
    const col = makeCol({ dataType: 'numeric', constraints: { precision: 10, scale: 2 } });
    const check: ParsedCheck = { column: 'col', operator: '>', value: 0 };

    it('generates values > 0', () => {
      const arb = applyCheckConstraint(fc.double(), check, col);
      fc.assert(fc.property(arb, v => Number(v) > 0));
    });

    it('generates values within NUMERIC(10,2) range', () => {
      const arb = applyCheckConstraint(fc.double(), check, col);
      fc.assert(fc.property(arb, v => Number(v) <= 99_999_999));
    });

    it('generates values rounded to 2 decimal places', () => {
      const arb = applyCheckConstraint(fc.double(), check, col);
      fc.assert(
        fc.property(arb, v => {
          const parts = String(v).split('.');
          return parts.length === 1 || parts[1]!.length <= 2;
        }),
      );
    });
  });

  describe('CHECK (col >= N) on NUMERIC(10,2)', () => {
    const col = makeCol({ dataType: 'numeric', constraints: { precision: 10, scale: 2 } });
    const check: ParsedCheck = { column: 'col', operator: '>=', value: 0 };

    it('generates values >= 0', () => {
      const arb = applyCheckConstraint(fc.double(), check, col);
      fc.assert(fc.property(arb, v => Number(v) >= 0));
    });
  });

  describe('CHECK (col < N) on NUMERIC(5,2)', () => {
    const col = makeCol({ dataType: 'numeric', constraints: { precision: 5, scale: 2 } });
    const check: ParsedCheck = { column: 'col', operator: '<', value: 100 };

    it('generates values < 100', () => {
      const arb = applyCheckConstraint(fc.double(), check, col);
      fc.assert(fc.property(arb, v => Number(v) < 100));
    });

    it('generates values within NUMERIC(5,2) range (max 999)', () => {
      const arb = applyCheckConstraint(fc.double(), check, col);
      fc.assert(fc.property(arb, v => Math.abs(Number(v)) <= 999));
    });
  });

  describe('CHECK (col <= N) on NUMERIC(10,2)', () => {
    const col = makeCol({ dataType: 'numeric', constraints: { precision: 10, scale: 2 } });
    const check: ParsedCheck = { column: 'col', operator: '<=', value: 50 };

    it('generates values <= 50', () => {
      const arb = applyCheckConstraint(fc.double(), check, col);
      fc.assert(fc.property(arb, v => Number(v) <= 50));
    });
  });

  describe('CHECK (col > N) on INTEGER', () => {
    const col = makeCol({ dataType: 'integer' });
    const check: ParsedCheck = { column: 'col', operator: '>', value: 0 };

    it('generates integers > 0', () => {
      const arb = applyCheckConstraint(fc.integer(), check, col);
      fc.assert(fc.property(arb, v => Number(v) >= 1 && Number.isInteger(Number(v))));
    });
  });

  describe('CHECK (col >= N) on INTEGER', () => {
    const col = makeCol({ dataType: 'integer' });
    const check: ParsedCheck = { column: 'col', operator: '>=', value: 0 };

    it('generates integers >= 0', () => {
      const arb = applyCheckConstraint(fc.integer(), check, col);
      fc.assert(fc.property(arb, v => Number(v) >= 0));
    });
  });

  describe('CHECK col BETWEEN x AND y', () => {
    const col = makeCol({ dataType: 'integer' });
    const check: ParsedCheck = { column: 'col', operator: 'BETWEEN', value: 1, value2: 100 };

    it('generates integers in [1, 100]', () => {
      const arb = applyCheckConstraint(fc.integer(), check, col);
      fc.assert(fc.property(arb, v => Number(v) >= 1 && Number(v) <= 100));
    });
  });

  describe('CHECK col BETWEEN x AND y on NUMERIC', () => {
    const col = makeCol({ dataType: 'numeric', constraints: { precision: 5, scale: 2 } });
    const check: ParsedCheck = { column: 'col', operator: 'BETWEEN', value: 0.5, value2: 99.99 };

    it('generates values in [0.5, 99.99]', () => {
      const arb = applyCheckConstraint(fc.double(), check, col);
      fc.assert(fc.property(arb, v => Number(v) >= 0.5 && Number(v) <= 99.99));
    });
  });

  describe('CHECK col IN (...)', () => {
    const col = makeCol({ dataType: 'text' });
    const check: ParsedCheck = { column: 'col', operator: 'IN', value: ['a', 'b', 'c'] };

    it('generates only values from the set', () => {
      const arb = applyCheckConstraint(fc.string(), check, col);
      fc.assert(fc.property(arb, v => ['a', 'b', 'c'].includes(String(v))));
    });
  });

  describe('CHECK col = N', () => {
    const col = makeCol({ dataType: 'integer' });
    const check: ParsedCheck = { column: 'col', operator: '=', value: 42 };

    it('always generates the constant value', () => {
      const arb = applyCheckConstraint(fc.integer(), check, col);
      fc.assert(fc.property(arb, v => v === 42));
    });
  });

  describe('non-numeric type with > falls back to filter', () => {
    const col = makeCol({ dataType: 'text' });
    const check: ParsedCheck = { column: 'col', operator: '>', value: 5 };

    it('does not throw', () => {
      const arb = applyCheckConstraint(fc.integer({ min: 0, max: 100 }), check, col);
      const samples = fc.sample(arb, 50);
      expect(samples.every(v => Number(v) > 5)).toBe(true);
    });
  });
});

// ---------------------------------------------------------------------------
// makeForeignKeyArbitrary
// ---------------------------------------------------------------------------

describe('makeForeignKeyArbitrary', () => {
  it('returns null when no parent rows exist', () => {
    const arb = makeForeignKeyArbitrary([], 'id');
    fc.assert(fc.property(arb, v => v === null));
  });

  it('picks from parent row values', () => {
    const parents = [{ id: 10 }, { id: 20 }, { id: 30 }];
    const arb = makeForeignKeyArbitrary(parents, 'id');
    fc.assert(fc.property(arb, v => [10, 20, 30].includes(Number(v))));
  });

  it('handles string referenced columns', () => {
    const parents = [{ code: 'US' }, { code: 'CA' }, { code: 'UK' }];
    const arb = makeForeignKeyArbitrary(parents, 'code');
    fc.assert(fc.property(arb, v => ['US', 'CA', 'UK'].includes(String(v))));
  });
});

// ---------------------------------------------------------------------------
// makeForeignKeyArbitrary – distribution strategies
// ---------------------------------------------------------------------------

describe('makeForeignKeyArbitrary – distribution strategies', () => {
  const parents = [{ id: 1 }, { id: 2 }, { id: 3 }, { id: 4 }, { id: 5 }];

  it('uniform: picks only values from the parent set', () => {
    const arb = makeForeignKeyArbitrary(parents, 'id', 'uniform');
    fc.assert(fc.property(arb, v => [1, 2, 3, 4, 5].includes(Number(v))));
  });

  it('zipf: picks only values from the parent set', () => {
    const arb = makeForeignKeyArbitrary(parents, 'id', 'zipf');
    fc.assert(fc.property(arb, v => [1, 2, 3, 4, 5].includes(Number(v))));
  });

  it('zipf: first parent appears more often than last (skewed)', () => {
    const arb = makeForeignKeyArbitrary(parents, 'id', 'zipf');
    const samples = fc.sample(arb, 2000);
    const countFirst = samples.filter(v => Number(v) === 1).length;
    const countLast = samples.filter(v => Number(v) === 5).length;
    // With Zipf 1/k weights, index 0 has weight 1000, index 4 has weight 200
    // First should appear at least 2x more often than last
    expect(countFirst).toBeGreaterThan(countLast * 2);
  });

  it('adversarial: only picks first, middle, and last values', () => {
    const arb = makeForeignKeyArbitrary(parents, 'id', 'adversarial');
    const samples = fc.sample(arb, 200);
    // first=1 (idx 0), middle=3 (idx 2), last=5 (idx 4)
    const valid = new Set([1, 3, 5]);
    expect(samples.every(v => valid.has(Number(v)))).toBe(true);
  });

  it('defaults to uniform when no strategy argument given', () => {
    const arb = makeForeignKeyArbitrary(parents, 'id');
    fc.assert(fc.property(arb, v => [1, 2, 3, 4, 5].includes(Number(v))));
  });

  it('returns null when parent list is empty regardless of strategy', () => {
    for (const strategy of ['uniform', 'zipf', 'adversarial'] as const) {
      const arb = makeForeignKeyArbitrary([], 'id', strategy);
      fc.assert(fc.property(arb, v => v === null));
    }
  });
});

// ---------------------------------------------------------------------------
// isIntegerType / isFloatType
// ---------------------------------------------------------------------------

describe('isIntegerType', () => {
  const intTypes = ['integer', 'int4', 'int', 'smallint', 'int2', 'bigint', 'int8',
                    'serial', 'smallserial', 'bigserial'];
  for (const t of intTypes) {
    it(`classifies "${t}" as integer`, () => {
      expect(isIntegerType(t)).toBe(true);
    });
  }

  it('rejects non-integer types', () => {
    expect(isIntegerType('numeric')).toBe(false);
    expect(isIntegerType('text')).toBe(false);
    expect(isIntegerType('boolean')).toBe(false);
  });
});

describe('isFloatType', () => {
  const floatTypes = ['real', 'float4', 'float8', 'double precision', 'float',
                      'numeric', 'decimal'];
  for (const t of floatTypes) {
    it(`classifies "${t}" as float`, () => {
      expect(isFloatType(t)).toBe(true);
    });
  }

  it('rejects non-float types', () => {
    expect(isFloatType('integer')).toBe(false);
    expect(isFloatType('text')).toBe(false);
    expect(isFloatType('boolean')).toBe(false);
  });
});
