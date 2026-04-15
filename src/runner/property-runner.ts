import * as fc from 'fast-check';
import type {
  SqlProofClient,
  Dataset,
  SchemaInfo,
  CheckOptions,
  TableCustomization,
} from '../schema/types.js';
import { makeDatasetArbitrary } from '../generators/dataset-generator.js';
import { DBManager } from './db-manager.js';
import { formatCounterexample } from '../reporter/reporter.js';

/**
 * Runs a property-based test against a live database.
 * The DBManager must already be started; it is NOT stopped here.
 */
export async function runChecks(
  name: string,
  options: CheckOptions,
  dbManager: DBManager,
  schemaInfo: SchemaInfo,
  customizations: Map<string, TableCustomization>,
): Promise<void> {
  const { generate, setup, property, runs = 100, seed, timeout = 5000 } = options;

  const datasetArb = makeDatasetArbitrary(schemaInfo, generate, customizations);

  let failureMessage: string | undefined;

  try {
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

          try {
            if (setup) {
              await setup(sqlProofClient);
            }

            const result = await Promise.race([
              property(sqlProofClient),
              new Promise<boolean>((_, reject) =>
                setTimeout(
                  () => reject(new Error(`Property timed out after ${timeout}ms`)),
                  timeout,
                ),
              ),
            ]);

            return result;
          } finally {
            client.release();
          }
        } finally {
          await dbManager.dropSchema(schemaName).catch(() => {});
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
    if (failureMessage) {
      const error = new Error(failureMessage);
      error.name = 'SqlProofError';
      throw error;
    }
    throw err;
  }
}
