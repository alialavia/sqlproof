import * as fc from 'fast-check';
import type { ColumnInfo, ParsedCheck } from '../schema/types.js';

/**
 * Wraps a nullable column's arbitrary with fc.option so it sometimes produces null.
 */
export function applyNullability(
  arb: fc.Arbitrary<unknown>,
  column: ColumnInfo,
): fc.Arbitrary<unknown> {
  if (!column.nullable) return arb;
  return fc.option(arb, { nil: null, freq: 5 }) as fc.Arbitrary<unknown>;
}

/**
 * Narrows a column's arbitrary to satisfy a parsed CHECK constraint.
 * Falls back to .filter() for operators that can't be expressed as bounded ranges.
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

function getNumericBounds(column: ColumnInfo): { numericMax: number; scale: number | undefined } {
  const t = column.dataType.toLowerCase();
  if (t === 'numeric' || t === 'decimal') {
    const s = column.constraints.scale ?? 2;
    const p = column.constraints.precision ?? 10;
    return { numericMax: Math.pow(10, p - s) - 1, scale: s };
  }
  return { numericMax: Number.MAX_SAFE_INTEGER, scale: undefined };
}

function makeNumericArb(
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
 */
export function makeForeignKeyArbitrary(
  parentRows: Record<string, unknown>[],
  referencedColumn: string,
): fc.Arbitrary<unknown> {
  if (parentRows.length === 0) {
    // No parent rows — FK column will be null (may cause insert failure for NOT NULL FKs,
    // but there's nothing valid to reference; caller should handle)
    return fc.constant(null);
  }
  const values = parentRows.map(r => r[referencedColumn]);
  return fc.constantFrom(...(values as [unknown, ...unknown[]]));
}

// ---------------------------------------------------------------------------
// Type helpers
// ---------------------------------------------------------------------------

function isIntegerType(dataType: string): boolean {
  const t = dataType.toLowerCase();
  return ['integer', 'int4', 'int', 'smallint', 'int2', 'bigint', 'int8',
          'serial', 'smallserial', 'bigserial'].includes(t);
}

function isFloatType(dataType: string): boolean {
  const t = dataType.toLowerCase();
  return ['real', 'float4', 'float8', 'double precision', 'float',
          'numeric', 'decimal'].includes(t);
}

/**
 * Fallback evaluator for CHECK constraints that couldn't be narrowed at generation time.
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
