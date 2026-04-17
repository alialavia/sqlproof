---
title: Security & Credentials
description: How to manage database credentials safely when using SqlProof.
---

## Never Use Production Databases

SqlProof creates and drops schemas automatically during test runs. Always point it at a dedicated test or staging database — never production.

| Mode | Risk level | Notes |
|------|-----------|-------|
| Testcontainers | None | Fully isolated throwaway container |
| Neon branching | None | Isolated branch, deleted after tests |
| Connection string | Low if configured correctly | Use a restricted role, staging DB only |

## Use Environment Variables

Never hardcode credentials in source files. Load them from environment variables:

```typescript
const proof = await SqlProof.connect({
  connectionString: process.env.DATABASE_URL!,
});
```

For local development, create a `.env` file and load it with a tool like [`dotenv`](https://github.com/motdotla/dotenv) or Vitest's `--env-file` flag:

```bash
# .env
DATABASE_URL=postgresql://sqlproof_test:yourpassword@localhost:5432/testdb
NEON_API_KEY=your-neon-api-key
NEON_PROJECT_ID=your-neon-project-id
```

## Protect Your `.env` File

Add to your `.gitignore` to prevent accidental commits:

```
.env
.env.local
.env.*.local
```

## Minimal Database Role

When using `connectionString`, create a dedicated role with only the permissions SqlProof needs. SqlProof only requires the ability to create and drop schemas within an existing database:

```sql
CREATE ROLE sqlproof_test LOGIN PASSWORD 'strong-password';
GRANT CONNECT ON DATABASE testdb TO sqlproof_test;
GRANT CREATE ON DATABASE testdb TO sqlproof_test;
```

`CREATE ON DATABASE` allows creating schemas. SqlProof creates one isolated schema per test run (e.g. `run_a3f8b2c1`) and drops it when the run finishes. No other privileges are required.

Do **not** grant `SUPERUSER` or `CREATEDB` — they are unnecessary and increase risk.

## Neon API Keys

Neon API keys can be scoped to a single project. Use project-scoped keys to limit what an exposed key can do:

1. Go to Neon Console → your project → **Settings → API Keys**
2. Click **Create key** and select project scope
3. Store the key as `NEON_API_KEY` in your CI secrets or `.env`

Rotate keys regularly. Revoke them immediately if exposed. A leaked project-scoped key can only affect that Neon project — not your entire account.

## CI/CD Secrets

Store all database credentials as encrypted secrets in your CI provider — never as plain environment variables in workflow files. See the [CI/CD Integration](/guides/ci-cd) guide for provider-specific instructions.
