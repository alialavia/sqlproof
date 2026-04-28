import { describe, expect, it } from 'vitest';
import * as fc from 'fast-check';
import { rewriteDefaultValue } from '../../src/runner/default-value-rewriter.js';
import type { SchemaInfo } from '../../src/schema/types.js';

const identifier = fc.stringMatching(/^[A-Za-z_][A-Za-z0-9_]{0,20}$/);

const sqlStringLiteral = fc
  .string({ maxLength: 30 })
  .filter(value => !value.includes("'"))
  .map(value => `'${value}'`);

function schemaWithEnum(enumName: string): SchemaInfo {
  return {
    tables: [],
    enums: [{ name: enumName, values: ['pending', 'active'] }],
  };
}

describe('rewriteDefaultValue', () => {
  it('rewrites schema-qualified enum casts to the target schema', () => {
    fc.assert(
      fc.property(
        identifier,
        identifier,
        identifier,
        sqlStringLiteral,
        (targetSchema, oldSchema, enumName, value) => {
          const input = `${value}::${oldSchema}.${enumName}`;

          expect(rewriteDefaultValue(input, targetSchema, schemaWithEnum(enumName))).toBe(
            `${value}::"${targetSchema}"."${enumName}"`,
          );
        },
      ),
    );
  });

  it('rewrites unqualified enum casts to the target schema', () => {
    fc.assert(
      fc.property(identifier, identifier, sqlStringLiteral, (targetSchema, enumName, value) => {
        const input = `${value}::${enumName}`;

        expect(rewriteDefaultValue(input, targetSchema, schemaWithEnum(enumName))).toBe(
          `${value}::"${targetSchema}"."${enumName}"`,
        );
      }),
    );
  });

  it('rewrites quoted enum casts to the target schema', () => {
    fc.assert(
      fc.property(
        identifier,
        identifier,
        identifier,
        sqlStringLiteral,
        (targetSchema, oldSchema, enumName, value) => {
          const input = `${value}::"${oldSchema}"."${enumName}"`;

          expect(rewriteDefaultValue(input, targetSchema, schemaWithEnum(enumName))).toBe(
            `${value}::"${targetSchema}"."${enumName}"`,
          );
        },
      ),
    );
  });

  it('leaves non-enum casts unchanged', () => {
    fc.assert(
      fc.property(
        identifier,
        fc.constantFrom('integer', 'text', 'numeric', 'uuid'),
        sqlStringLiteral,
        (targetSchema, typeName, value) => {
          const input = `${value}::${typeName}`;

          expect(rewriteDefaultValue(input, targetSchema, schemaWithEnum('order_status'))).toBe(
            input,
          );
        },
      ),
    );
  });

  it('does not rewrite enum-name prefixes in other type names', () => {
    fc.assert(
      fc.property(identifier, sqlStringLiteral, (targetSchema, value) => {
        const input = `${value}::order_status_archive`;

        expect(rewriteDefaultValue(input, targetSchema, schemaWithEnum('order_status'))).toBe(
          input,
        );
      }),
    );
  });
});
