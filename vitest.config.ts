import { defineConfig } from 'vitest/config';
export default defineConfig({
  test: {
    globals: false,
    environment: 'node',
    pool: 'forks',
    poolOptions: { forks: { maxForks: 1 } },
    testTimeout: 30000,
    include: ['tests/**/*.test.ts', 'examples/**/*.test.ts'],
  },
});
