import * as fc from 'fast-check';
import type { ColumnInfo, EnumInfo } from '../schema/types.js';

/**
 * Returns a fast-check Arbitrary for the given column's data type.
 * For SERIAL / GENERATED columns, returns fc.constant(null) as a sentinel.
 * Callers should check column.isGenerated and skip those columns in INSERTs.
 */
export function getArbitraryForColumn(
  column: ColumnInfo,
  enums: EnumInfo[],
): fc.Arbitrary<unknown> {
  if (column.isGenerated) return fc.constant(null);

  if (column.isArray) {
    const baseArb = getBaseArbitrary(column.baseType ?? column.dataType, column, enums);
    return fc.array(baseArb, { maxLength: 5 }).map(arr => JSON.stringify(arr));
  }

  return getBaseArbitrary(column.dataType, column, enums);
}

function getBaseArbitrary(
  dataType: string,
  column: ColumnInfo,
  enums: EnumInfo[],
): fc.Arbitrary<unknown> {
  const t = dataType.toLowerCase().trim();

  switch (t) {
    case 'integer':
    case 'int4':
    case 'int':
      return fc.integer({ min: -2147483648, max: 2147483647 });

    case 'smallint':
    case 'int2':
      return fc.integer({ min: -32768, max: 32767 });

    case 'bigint':
    case 'int8':
      // Serialize as string for pg driver compatibility
      return fc
        .bigInt({ min: BigInt('-9223372036854775808'), max: BigInt('9223372036854775807') })
        .map(n => n.toString());

    case 'serial':
    case 'serial4':
      return fc.integer({ min: 1, max: 2147483647 });

    case 'smallserial':
    case 'serial2':
      return fc.integer({ min: 1, max: 32767 });

    case 'bigserial':
    case 'serial8':
      return fc.integer({ min: 1, max: 2147483647 });

    case 'real':
    case 'float4':
      return fc.float({ noNaN: true, noDefaultInfinity: true });

    case 'double precision':
    case 'float8':
    case 'float':
      return fc.double({ noNaN: true, noDefaultInfinity: true });

    case 'numeric':
    case 'decimal': {
      const scale = column.constraints.scale ?? 2;
      const precision = column.constraints.precision ?? 10;
      const maxVal = Math.pow(10, precision - scale) - 1;
      return fc
        .double({ min: -maxVal, max: maxVal, noNaN: true, noDefaultInfinity: true })
        .map(n => parseFloat(n.toFixed(scale)));
    }

    case 'boolean':
    case 'bool':
      return fc.boolean();

    case 'text':
      return fc.string({ unit: 'grapheme', maxLength: 255 });

    case 'varchar':
    case 'character varying': {
      const maxLen = column.constraints.length ?? 255;
      return fc.string({ unit: 'grapheme', maxLength: maxLen });
    }

    case 'char':
    case 'character':
    case 'bpchar': {
      const len = column.constraints.length ?? 1;
      return fc.string({ unit: 'grapheme', minLength: len, maxLength: len });
    }

    case 'uuid':
      return fc.uuid();

    case 'timestamp':
    case 'timestamp without time zone':
      return fc
        .date({
          noInvalidDate: true,
          min: new Date('1970-01-01T00:00:00.000Z'),
          max: new Date('2099-12-31T23:59:59.999Z'),
        })
        .map(d => d.toISOString().replace('T', ' ').slice(0, 19));

    case 'timestamptz':
    case 'timestamp with time zone':
      return fc
        .date({
          noInvalidDate: true,
          min: new Date('1970-01-01T00:00:00.000Z'),
          max: new Date('2099-12-31T23:59:59.999Z'),
        })
        .map(d => d.toISOString());

    case 'date':
      return fc
        .date({
          noInvalidDate: true,
          min: new Date('1970-01-01T00:00:00.000Z'),
          max: new Date('2099-12-31T23:59:59.999Z'),
        })
        .map(d => d.toISOString().split('T')[0]!);

    case 'time':
    case 'time without time zone': {
      const hour = fc.integer({ min: 0, max: 23 });
      const min = fc.integer({ min: 0, max: 59 });
      const sec = fc.integer({ min: 0, max: 59 });
      return fc.tuple(hour, min, sec).map(([h, m, s]) =>
        `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`,
      );
    }

    case 'timetz':
    case 'time with time zone': {
      const hour = fc.integer({ min: 0, max: 23 });
      const min = fc.integer({ min: 0, max: 59 });
      const sec = fc.integer({ min: 0, max: 59 });
      return fc.tuple(hour, min, sec).map(([h, m, s]) =>
        `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}+00:00`,
      );
    }

    case 'json':
    case 'jsonb':
      return fc.jsonValue().map(v => JSON.stringify(v));

    case 'bytea':
      return fc.string({ unit: 'grapheme', maxLength: 100 }).map(s => Buffer.from(s));

    default: {
      // Check if it's a known enum
      const enumDef = enums.find(e => e.name.toLowerCase() === t);
      if (enumDef && enumDef.values.length > 0) {
        return fc.constantFrom(...(enumDef.values as [string, ...string[]]));
      }
      // Unknown type — return empty string and warn
      console.warn(`[sqlproof] Unknown data type "${dataType}" for column "${column.name}", using empty string`);
      return fc.constant('');
    }
  }
}
