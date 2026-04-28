import type { SchemaInfo } from '../schema/types.js';

/**
 * Postgres introspection returns enum defaults as schema-qualified casts, e.g.
 * `'pending'::_sqlproof_introspect_x.order_status`.
 *
 * SqlProof drops that introspection schema before property runs, then rebuilds
 * table DDL inside each `run_<id>` schema. Enum default casts must therefore be
 * retargeted to the current run schema or CREATE TABLE can reference a dropped
 * type.
 *
 * Only enum casts known from SchemaInfo are rewritten; other default
 * expressions are preserved.
 */
export function rewriteDefaultValue(
  defaultValue: string,
  schemaName: string,
  schemaInfo: SchemaInfo,
): string {
  let rewritten = defaultValue;

  for (const enumInfo of schemaInfo.enums) {
    const enumName = escapeRegExp(enumInfo.name);
    const schemaQualifiedCast = new RegExp(
      `::(?:"[^"]+"|[\\w$]+)\\.(?:"${enumName}"|${enumName})(?=$|[^\\w$])`,
      'gi',
    );
    const unqualifiedCast = new RegExp(
      `::(?:"${enumName}"|${enumName})(?=$|[^\\w$.])`,
      'gi',
    );
    const replacement = `::"${schemaName}"."${enumInfo.name}"`;

    rewritten = rewritten
      .replace(schemaQualifiedCast, replacement)
      .replace(unqualifiedCast, replacement);
  }

  return rewritten;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
