const TEST_DATABASE_ENV = 'SQLPROOF_TEST_DATABASE_URL';

export function getTestDatabaseUrl(env: NodeJS.ProcessEnv = process.env): string {
  const url = env[TEST_DATABASE_ENV];
  if (!url) {
    throw new Error(
      `Set ${TEST_DATABASE_ENV} to run Postgres-backed tests. ` +
        'For local Supabase, use postgresql://postgres:postgres@127.0.0.1:54322/postgres.',
    );
  }
  return url;
}
