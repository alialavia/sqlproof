---
title: Local Development
description: Set up SqlProof locally with any of the three connection modes.
---

## Mode 1: Testcontainers (Zero Config)

The easiest way to get started. Requires [Docker Desktop](https://www.docker.com/products/docker-desktop/).

Install the testcontainers peer dependency:

```bash
npm install -D @testcontainers/postgresql
```

Then use `schemaFile`:

```typescript
const proof = await SqlProof.connect({
  schemaFile: './schema.sql',
});
```

SqlProof pulls `postgres:16` on first run and caches the container with `.withReuse()` — subsequent runs start in milliseconds.

### Troubleshooting

**`Cannot connect to the Docker daemon`**
Docker Desktop is not running. Start it from your Applications folder or system tray.

**First run takes a long time**
Docker is pulling the `postgres:16` image. This only happens once — subsequent runs reuse the cached container.

**Port conflicts**
Testcontainers assigns a random host port automatically. If you see a port binding error, ensure Docker's default bridge network isn't blocked by a VPN or firewall.

**RYUK container errors**
In CI environments with restricted Docker access, set:
```
TESTCONTAINERS_RYUK_DISABLED=true
```

---

## Mode 2: Local Postgres

If you have Postgres installed locally, or prefer to manage the container yourself:

```bash
# Start a local Postgres container
docker run -d --name pg-test \
  -e POSTGRES_PASSWORD=test \
  -p 5432:5432 \
  postgres:16
```

Create a `.env` file in your project root:

```
DATABASE_URL=postgresql://postgres:test@localhost:5432/postgres
```

Load it in your tests (e.g. using [`dotenv`](https://github.com/motdotla/dotenv)):

```typescript
import 'dotenv/config';

const proof = await SqlProof.connect({
  connectionString: process.env.DATABASE_URL!,
  schemaFile: './schema.sql',  // no Docker needed — uses your local Postgres
});
```

Or to introspect an existing live schema:

```typescript
const proof = await SqlProof.connect({
  connectionString: process.env.DATABASE_URL!,
  schema: 'public',
});
```

---

## Mode 3: Neon Branching Locally

[Neon](https://neon.tech) offers a generous free tier and instant database branches — no local Docker needed.

1. Sign up at [neon.tech](https://neon.tech) (free)
2. Create a project and note the **Project ID**
3. Apply your schema to the default `main` branch (via Neon SQL editor or `psql`)
4. Generate a project-scoped API key: Neon Console → Settings → API Keys

Create a `.env` file:

```
NEON_API_KEY=your-api-key-here
NEON_PROJECT_ID=your-project-id-here
```

In your tests:

```typescript
import 'dotenv/config';

const proof = await SqlProof.connect({
  neon: {
    apiKey: process.env.NEON_API_KEY!,
    projectId: process.env.NEON_PROJECT_ID!,
    parentBranch: 'main', // branch from your schema-ready branch
  },
  schema: 'public',
});
```

Each `SqlProof.connect()` creates a new Neon branch (~1 second). `disconnect()` deletes it. Free Neon accounts support up to 10 branches simultaneously.

---

## Vitest Configuration

All modes require `pool: 'forks'`:

```typescript
// vitest.config.ts
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    pool: 'forks',
    testTimeout: 120_000,
  },
});
```
