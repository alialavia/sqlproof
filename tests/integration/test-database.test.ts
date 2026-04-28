import { describe, expect, it } from 'vitest';
import { getTestDatabaseUrl } from './test-database.js';

describe('getTestDatabaseUrl', () => {
  it('fails clearly when SQLPROOF_TEST_DATABASE_URL is missing', () => {
    expect(() => getTestDatabaseUrl({})).toThrow(
      'Set SQLPROOF_TEST_DATABASE_URL to run Postgres-backed tests.',
    );
  });

  it('reads the Postgres test database URL from SQLPROOF_TEST_DATABASE_URL', () => {
    expect(
      getTestDatabaseUrl({
        SQLPROOF_TEST_DATABASE_URL: 'postgresql://postgres:postgres@127.0.0.1:54322/postgres',
      }),
    ).toBe('postgresql://postgres:postgres@127.0.0.1:54322/postgres');
  });
});
