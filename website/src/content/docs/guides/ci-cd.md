---
title: CI/CD Integration
description: GitHub Actions examples for the Python SqlProof workflow.
---

SqlProof works in CI anywhere Python and PostgreSQL are available. The repository
ships workflows for linting, type checking, coverage, packaging, and docs builds.

## Basic GitHub Actions

```yaml
name: Tests
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --extra dev
      - run: uv run ruff check src/ tests/
      - run: uv run pyright
      - run: uv run mypy src/sqlproof/
      - run: uv run pytest --cov=sqlproof --cov-fail-under=95
```

## With a Postgres Service

```yaml
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
```

Then pass the DSN to pytest:

```yaml
- run: uv run pytest
  env:
    DATABASE_URL: postgresql://postgres:ci@localhost:5432/testdb
```

## Useful Commands

```bash
uv run pytest
uv run pytest examples/ecommerce examples/ripenn_scoring
uv run pytest --cov=sqlproof --cov-fail-under=95
uv run ruff check src/ tests/ examples/
uv run pyright
uv run mypy src/sqlproof
uv build
```
