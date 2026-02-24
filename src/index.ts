import { runProperty } from './runner/property-runner.js';
import type { SqlProofCheckOptions } from './schema/types.js';

export const sqlproof = {
  check: (options: SqlProofCheckOptions): Promise<void> => runProperty(options),
};

export { runProperty };

export type {
  SqlProofCheckOptions,
  SqlProofClient,
  GeneratorOverrides,
  SchemaInfo,
  TableInfo,
  ColumnInfo,
  ForeignKeyInfo,
  CheckConstraint,
  ParsedCheck,
  EnumInfo,
  Dataset,
} from './schema/types.js';
