import * as fc from 'fast-check';
import type { ColumnInfo, ParsedCheck, FkDistributionStrategy } from '../schema/types.js';

/**
 * Wraps a nullable column's arbitrary with fc.option so it sometimes produces null.
 * freq: 5 means roughly 1 in 5 generated values will be null.
 */
export function applyNullability(
  arb: fc.Arbitrary<unknown>,
  column: ColumnInfo,
): fc.Arbitrary<unknown> {
  if (!column.nullable) return arb;
  return fc.option(arb, { nil: null, freq: 5 }) as fc.Arbitrary<unknown>;
}

/**
 * Replaces a column's arbitrary with one that satisfies a parsed CHECK constraint.
 *
 * For integer and numeric/decimal types we replace the arbitrary entirely (rather
 * than using .filter()) so fast-check can generate valid values directly and
 * shrink effectively. The replacement respects the column's NUMERIC(p,s) bounds
 * so values never overflow when inserted into Postgres.
 *
 * For non-numeric types or unrecognised operators we fall back to .filter(),
 * which is slower but always correct.
 */
export function applyCheckConstraint(
  arb: fc.Arbitrary<unknown>,
  check: ParsedCheck,
  column: ColumnInfo,
): fc.Arbitrary<unknown> {
  const isIntType = isIntegerType(column.dataType);
  const isNumType = isIntType || isFloatType(column.dataType);
  const { numericMax, scale } = getNumericBounds(column);

  switch (check.operator) {
    case 'IN': {
      const values = check.value as unknown[];
      if (values.length === 0) return arb;
      return fc.constantFrom(...(values as [unknown, ...unknown[]]));
    }

    case 'BETWEEN': {
      const lo = Number(check.value);
      const hi = Number(check.value2);
      if (isIntType) {
        return fc.integer({ min: Math.ceil(lo), max: Math.floor(hi) });
      }
      return makeNumericArb(lo, hi, scale);
    }

    case '>': {
      const val = Number(check.value);
      if (isIntType) {
        return fc.integer({ min: Math.floor(val) + 1, max: 2147483647 });
      }
      if (isNumType) {
        // Advance past the boundary by the smallest representable step at
        // this scale. For NUMERIC(10,2) with CHECK (price > 0), step is
        // 0.01 so the minimum generated value is 0.01 — the smallest
        // value that survives rounding and still satisfies > 0.
        const step = scale != null ? Math.pow(10, -scale) : 0.000001;
        return makeNumericArb(val + step, numericMax, scale);
      }
      return arb.filter(v => Number(v) > val);
    }

    case '>=': {
      const val = Number(check.value);
      if (isIntType) {
        return fc.integer({ min: Math.ceil(val), max: 2147483647 });
      }
      if (isNumType) {
        return makeNumericArb(val, numericMax, scale);
      }
      return arb.filter(v => Number(v) >= val);
    }

    case '<': {
      const val = Number(check.value);
      if (isIntType) {
        return fc.integer({ min: -2147483648, max: Math.ceil(val) - 1 });
      }
      if (isNumType) {
        const step = scale != null ? Math.pow(10, -scale) : 0.000001;
        return makeNumericArb(-numericMax, val - step, scale);
      }
      return arb.filter(v => Number(v) < val);
    }

    case '<=': {
      const val = Number(check.value);
      if (isIntType) {
        return fc.integer({ min: -2147483648, max: Math.floor(val) });
      }
      if (isNumType) {
        return makeNumericArb(-numericMax, val, scale);
      }
      return arb.filter(v => Number(v) <= val);
    }

    case '=': {
      return fc.constant(check.value);
    }

    default:
      return arb.filter(v => evaluateSimpleCheck(v, check));
  }
}

/**
 * Returns the maximum absolute integer part and decimal scale for a column.
 *
 * PostgreSQL's NUMERIC(precision, scale) stores at most `precision` total
 * digits, `scale` of which are after the decimal point. The integer part
 * therefore has at most `precision - scale` digits, giving a maximum value
 * of 10^(precision - scale) - 1.
 *
 * Example: NUMERIC(10,2) → 10-2 = 8 integer digits → max 99_999_999.
 *
 * Defaults when the column was declared without explicit parameters:
 *  - scale ?? 2  — Postgres defaults to 0, but bare "NUMERIC" allows
 *    arbitrary scale; we use 2 as a practical default since most real-world
 *    schemas use NUMERIC for money/prices (2 decimal places).
 *  - precision ?? 10 — arbitrary-precision NUMERIC can hold huge values,
 *    but generating values up to 10^10 is a reasonable test range that
 *    exercises realistic data without overflowing application code.
 *
 * For non-NUMERIC types (real, double precision, etc.) we return
 * MAX_SAFE_INTEGER with no scale, producing unconstrained doubles.
 */
export function getNumericBounds(column: ColumnInfo): { numericMax: number; scale: number | undefined } {
  const t = column.dataType.toLowerCase();
  if (t === 'numeric' || t === 'decimal') {
    const s = column.constraints.scale ?? 2;
    const p = column.constraints.precision ?? 10;
    return { numericMax: Math.pow(10, p - s) - 1, scale: s };
  }
  return { numericMax: Number.MAX_SAFE_INTEGER, scale: undefined };
}

/**
 * Builds an fc.double arbitrary clamped to [min, max], optionally rounded to
 * `scale` decimal places. The rounding mirrors what Postgres does on INSERT
 * for NUMERIC(p,s) columns, so the generated value matches what ends up in
 * the database. Without this, a value like 0.000001 would be rounded to 0.00
 * by Postgres, silently violating a CHECK (price > 0) constraint.
 */
export function makeNumericArb(
  min: number,
  max: number,
  scale: number | undefined,
): fc.Arbitrary<unknown> {
  const arb = fc.double({ min, max, noNaN: true, noDefaultInfinity: true });
  if (scale != null) {
    return arb.map(n => parseFloat(n.toFixed(scale)));
  }
  return arb;
}

/**
 * Creates an arbitrary that picks FK values from already-generated parent rows.
 *
 * distribution strategies:
 *   uniform    – equal probability per parent (default)
 *   zipf       – skewed: earlier parents get exponentially more children
 *   adversarial – only picks first, middle, and last parents (boundary stress)
 *
 * Returns fc.constant(null) when parentRows is empty.
 */
export function makeForeignKeyArbitrary(
  parentRows: Record<string, unknown>[],
  referencedColumn: string,
  distribution: FkDistributionStrategy = 'uniform',
): fc.Arbitrary<unknown> {
  if (parentRows.length === 0) {
    return fc.constant(null);
  }

  const values = parentRows.map(r => r[referencedColumn]);

  switch (distribution) {
    case 'zipf': {
      // Weight for index i is ceil(1000 / (i+1)): index 0 → 1000, index 1 → 500, etc.
      const entries = values.map((val, i) => ({
        weight: Math.ceil(1000 / (i + 1)),
        arbitrary: fc.constant(val),
      })) as [{ weight: number; arbitrary: fc.Arbitrary<unknown> }, ...{ weight: number; arbitrary: fc.Arbitrary<unknown> }[]];
      return fc.oneof(...entries);
    }

    case 'adversarial': {
      const first = values[0]!;
      const last = values[values.length - 1]!;
      const mid = values[Math.floor((values.length - 1) / 2)]!;
      const boundaries = [...new Set([first, mid, last])] as [unknown, ...unknown[]];
      return fc.constantFrom(...boundaries);
    }

    case 'uniform':
    default:
      return fc.constantFrom(...(values as [unknown, ...unknown[]]));
  }
}

// ---------------------------------------------------------------------------
// Type classification helpers
// ---------------------------------------------------------------------------

/** Returns true for all PostgreSQL integer family types including serials. */
export function isIntegerType(dataType: string): boolean {
  const t = dataType.toLowerCase();
  return ['integer', 'int4', 'int', 'smallint', 'int2', 'bigint', 'int8',
          'serial', 'smallserial', 'bigserial'].includes(t);
}

/** Returns true for PostgreSQL floating-point and arbitrary-precision types. */
export function isFloatType(dataType: string): boolean {
  const t = dataType.toLowerCase();
  return ['real', 'float4', 'float8', 'double precision', 'float',
          'numeric', 'decimal'].includes(t);
}

/**
 * Fallback runtime evaluator for CHECK constraints that couldn't be
 * expressed as bounded ranges at generation time. Used with .filter()
 * to reject values that violate the constraint.
 */
function evaluateSimpleCheck(v: unknown, check: ParsedCheck): boolean {
  switch (check.operator) {
    case '>': return Number(v) > Number(check.value);
    case '>=': return Number(v) >= Number(check.value);
    case '<': return Number(v) < Number(check.value);
    case '<=': return Number(v) <= Number(check.value);
    case '=': return v === check.value || String(v) === String(check.value);
    case 'IN': return (check.value as unknown[]).some(val => String(v) === String(val));
    case 'BETWEEN': return Number(v) >= Number(check.value) && Number(v) <= Number(check.value2);
    default: return true;
  }
}
