import { defineConfig } from 'vitest/config';
export default defineConfig({
  test: {
    globals: false,
    environment: 'node',
    pool: 'forks',
    poolOptions: { forks: { singleFork: true } },
    testTimeout: 30000,
    include: ['tests/**/*.test.ts', 'examples/**/*.test.ts'],
  },
});
