---
title: CI/CD Integration
description: GitHub Actions examples for all three SqlProof connection modes.
---

SqlProof works in any CI environment that can reach a PostgreSQL database. Choose the mode that fits your pipeline.

## Mode 1: Testcontainers (Docker-in-CI)

Requires Docker available in the runner. GitHub Actions' `ubuntu-latest` includes Docker.

```yaml
# .github/workflows/test.yml
name: Tests
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
      - run: npm ci
      - run: npm run test:integration
        env:
          TESTCONTAINERS_RYUK_DISABLED: 'true'
```

No additional secrets required — SqlProof pulls `postgres:16` automatically. Set `TESTCONTAINERS_RYUK_DISABLED=true` to avoid permission issues in restricted CI environments.

In your test file:

```typescript
const proof = await SqlProof.connect({
  schemaFile: './schema.sql',
});
```

## Mode 2: Connection String (Staging / CI Postgres)

Use a GitHub Actions service container for a real, ephemeral Postgres — no Docker socket tricks needed.

```yaml
name: Tests
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: ci
          POSTGRES_DB: testdb
        options: >-
          --health-cmd pg_isready
          --health-interval 5s
          --health-timeout 5s
          --health-retries 5
        ports:
          - 5432:5432
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
      - run: npm ci
      - run: npm run test:integration
        env:
          DATABASE_URL: postgresql://postgres:ci@localhost:5432/testdb
```

In your test file:

```typescript
const proof = await SqlProof.connect({
  connectionString: process.env.DATABASE_URL!,
  schemaFile: './schema.sql',   // applies DDL to the CI DB — no Docker needed
  // or: schema: 'public'       // introspect an existing live schema
});
```

## Mode 3: Neon Branching

Each CI run gets its own instant Neon branch (~1 second). No Docker, no service containers.

1. Create a [Neon](https://neon.tech) project and note the project ID.
2. Generate a project-scoped API key in Neon Console → Settings → API Keys.
3. Add `NEON_API_KEY` and `NEON_PROJECT_ID` as [repository secrets](https://docs.github.com/en/actions/security-guides/encrypted-secrets).

```yaml
name: Tests
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
      - run: npm ci
      - run: npm run test:integration
        env:
          NEON_API_KEY: ${{ secrets.NEON_API_KEY }}
          NEON_PROJECT_ID: ${{ secrets.NEON_PROJECT_ID }}
```

In your test file:

```typescript
const proof = await SqlProof.connect({
  neon: {
    apiKey: process.env.NEON_API_KEY!,
    projectId: process.env.NEON_PROJECT_ID!,
    parentBranch: 'main',  // optional — branch from your schema-ready branch
  },
  schema: 'public',
});
// Branch is created on connect(), deleted on disconnect()
```

The branch is deleted in `disconnect()` even if the test fails — cleanup is guaranteed.

## Vitest Configuration

All modes require `pool: 'forks'` in Vitest config (required by testcontainers and for process isolation):

```typescript
// vitest.config.ts
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    pool: 'forks',
    testTimeout: 120_000, // allow time for container startup or branch creation
  },
});
```
