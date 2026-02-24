import { defineConfig } from 'vitest/config';
export default defineConfig({
  test: {
    globals: false,
    environment: 'node',
    pool: 'forks',
    testTimeout: 30000,
    include: ['tests/**/*.test.ts'],
    exclude: ['tests/integration/**'],
  },
});
