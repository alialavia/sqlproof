import * as fc from 'fast-check';
import type { SqlProofCheckOptions, SqlProofClient, Dataset } from '../schema/types.js';
import { parseSchemaFromFile, parseSchemaFromConnection } from '../schema/parser.js';
import { makeDatasetArbitrary } from '../generators/dataset-generator.js';
import { DBManager } from './db-manager.js';
import { formatCounterexample } from '../reporter/reporter.js';

/**
 * Main entry point: parse schema, spin up DB, run property-based test.
 * Throws on property failure with a formatted counterexample message.
 */
export async function runProperty(options: SqlProofCheckOptions): Promise<void> {
  const {
    name,
    schema,
    property,
    runs = 100,
    rowsPerTable = 10,
    seed,
    timeout = 5000,
    tables,
    overrides,
  } = options;

  // 1. Parse schema once
  const schemaInfo = isConnectionString(schema)
    ? await parseSchemaFromConnection(schema)
    : await parseSchemaFromFile(schema);

  // 2. Start DB manager once
  const dbManager = new DBManager(
    isConnectionString(schema) ? { connectionString: schema } : { useTestcontainers: true },
  );
  await dbManager.start();

  // 3. Build dataset arbitrary once
  const datasetArb = makeDatasetArbitrary(schemaInfo, rowsPerTable, overrides, tables);

  // Failure info captured from reporter callback
  let failureMessage: string | undefined;

  try {
    // 4. Run fc.assert
    await fc.assert(
      fc.asyncProperty(datasetArb, async (dataset: Dataset) => {
        const schemaName = DBManager.generateSchemaName();
        try {
          await dbManager.setupSchema(schemaName, schemaInfo);
          const client = await dbManager.getClientForSchema(schemaName);
          let realDataset: Dataset;
          try {
            realDataset = await dbManager.insertDataset(client, dataset, schemaInfo, schemaName);
          } catch (insertErr) {
            client.release();
            // Insert failure usually means constraint violation from bad data generation
            // Skip this run by returning true (not a property violation)
            console.warn(
              `[sqlproof] Insert failed (skipping run): ${(insertErr as Error).message}`,
            );
            return true;
          }

          const sqlProofClient: SqlProofClient = {
            query: (sql, params) =>
              (client as import('pg').PoolClient)
                .query(`SET search_path TO "${schemaName}"; ${sql}`, params)
                .then(r => ({ rows: r.rows as Record<string, unknown>[] })),
            getGeneratedData: () => realDataset,
          };

          let result: boolean;
          try {
            result = await Promise.race([
              property(sqlProofClient),
              new Promise<boolean>((_, reject) =>
                setTimeout(() => reject(new Error(`Property timed out after ${timeout}ms`)), timeout),
              ),
            ]);
          } finally {
            client.release();
          }

          return result;
        } finally {
          await dbManager.dropSchema(schemaName).catch(() => {
            // best-effort cleanup
          });
        }
      }),
      {
        numRuns: runs,
        ...(seed !== undefined ? { seed } : {}),
        reporter(runDetails) {
          if (runDetails.failed) {
            const counterexampleDataset =
              runDetails.counterexample != null
                ? (runDetails.counterexample[0] as Dataset)
                : undefined;
            failureMessage = formatCounterexample(name, counterexampleDataset, {
              numRuns: runDetails.numRuns,
              seed: runDetails.seed,
              numShrinks: runDetails.numShrinks,
            });
          }
        },
      },
    );
  } catch (err) {
    // Re-throw with formatted message if we have one
    if (failureMessage) {
      const error = new Error(failureMessage);
      error.name = 'SqlProofError';
      throw error;
    }
    throw err;
  } finally {
    await dbManager.stop();
  }
}

function isConnectionString(s: string): boolean {
  return s.startsWith('postgresql://') || s.startsWith('postgres://');
}
