# SqlProof

[![CI](https://github.com/alialavia/sqlproof/actions/workflows/ci.yml/badge.svg)](https://github.com/alialavia/sqlproof/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/alialavia/sqlproof/branch/main/graph/badge.svg)](https://codecov.io/gh/alialavia/sqlproof)
[![PyPI](https://img.shields.io/pypi/v/sqlproof?include_prereleases&v=2)](https://pypi.org/project/sqlproof/)

Property-based testing for PostgreSQL. Describe an invariant about your schema
or SQL; SqlProof generates valid datasets with Hypothesis, runs your query
through `psycopg`, and saves the shrunk counterexample when something breaks.

**Full docs: [sqlproof.com](https://sqlproof.com)**

## Install

```bash
pip install sqlproof
# or:
uv add sqlproof
```

Requires Python 3.11+ and PostgreSQL 13+.

Running in CI? See [the CI/CD guide](https://sqlproof.com/guides/ci-cd/) for
copy-paste GitHub Actions workflows covering vanilla Postgres and the extra
setup Supabase-shaped projects need (auth migration, plpgsql_check).

## Quick start

Given `schema.sql`:

```sql
CREATE TABLE orders (
  id SERIAL PRIMARY KEY,
  customer_id INTEGER NOT NULL,
  total NUMERIC(10,2) NOT NULL CHECK (total >= 0)
);

CREATE TABLE line_items (
  id SERIAL PRIMARY KEY,
  order_id INTEGER NOT NULL REFERENCES orders(id),
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  price NUMERIC(10,2) NOT NULL CHECK (price > 0)
);
```

Write a property test with pytest:

```python
from sqlproof import SqlProof, sqlproof

proof = SqlProof.from_schema_file("./schema.sql")


@sqlproof(proof, sizes={"orders": 20, "line_items": 50}, runs=50)
def test_no_orphan_line_items(db):
    rows = db.query("""
        SELECT li.id
        FROM line_items li
        LEFT JOIN orders o ON li.order_id = o.id
        WHERE o.id IS NULL
    """)
    assert rows == []
```

SqlProof parses your schema, topologically orders tables by FK, generates rows
that honor types / CHECK / UNIQUE / NOT NULL / FK constraints, runs the
property under Hypothesis, and shrinks any failure to the smallest reproducer.

## What you can do

### Generate datasets that respect your schema

The generation engine reads your schema and produces multi-table datasets
where every FK references a real parent, every CHECK constraint is honored at
generation time (no rejection sampling), every UNIQUE constraint is enforced,
and types are realistic — `NUMERIC(10,2)` gets scale-2 decimals, `varchar(50)`
gets bounded strings, enums sample from declared values, `vector(N)` gets
length-correct embeddings.

Useful far beyond tests: seed local dev databases, generate fixtures, replay
schema-respecting data through migrations, sample child-row FKs from external
parent tables (e.g. Supabase `auth.users`).

→ Walkthrough with column overrides, derived values, shrinkable cardinalities,
and external parent tables: [Realistic Data Generation](https://sqlproof.com/examples/data-generation/).

### Catch invariant violations across hundreds of datasets

A pgTAP test asserts a specific value against a fixed fixture. A SqlProof
property describes an *invariant* and lets Hypothesis throw hundreds of valid
datasets at it — including edge cases (NULLs, decimal precision, empty groups,
tied window values) you wouldn't think to type. When a property fails,
Hypothesis shrinks the dataset to the smallest reproducer and saves it.

Common shapes that property tests cover much better than examples:

- **Aggregation invariants** — DB-side aggregate matches a Python recomputation across any input.
- **RLS policy regressions** — every role/membership/sharing combination yields the right visible rows.
- **Migration safety** — old query and new query produce the same answer for every dataset.
- **Idempotency** — operation applied twice = applied once.
- **Round-trip serialization** — JSONB / custom types survive serialize→parse intact.

→ Walkthroughs of all five: [Five Property Patterns](https://sqlproof.com/examples/property-patterns/).

→ The strongest case: testing **SQL functions** with stacked discounts,
country-specific tax, and rounding edge cases — pgTAP version side-by-side
with the SqlProof version, showing four realistic regressions where pgTAP
silently passes and SqlProof catches: [Testing SQL Functions — pgTAP vs SqlProof](https://sqlproof.com/examples/testing-sql-functions/).

→ Honest comparison with pgTAP: [SqlProof vs pgTAP](https://sqlproof.com/guides/vs-pgtap/).

### Generate ad-hoc fixtures from the same schema

Property tests cover most cases, but a few residuals — RLS regression pins,
HTTP-layer tests that need a fixture row to exist, pytest fixtures shared
across many examples — still want one specific row inserted ahead of time.
The reflex is to write a helper:

```python
# Anti-pattern: hand-rolled INSERT in a test helper.
def insert_project(db, owner_id, *, name):
    db.execute(
        "INSERT INTO projects (id, user_id, name) VALUES (%s, %s, %s)",
        new_id(), owner_id, name,
    )
```

This compiles fine, looks fine in review, and silently breaks the next time a
migration adds a NOT NULL column to `projects`. The failure surfaces commits
later, in tests that aren't nominally about projects, as a cryptic
`NotNullViolation`.

Use `SqlProof.row_strategy` instead — a thin, schema-aware wrapper over the
same generator the property runner uses:

```python
# Same fixture, schema-backed. When a migration adds `org_id NOT NULL`,
# this helper keeps working — the generator fills the new column.
def insert_project(db, owner_id, *, name):
    row = proof.row_strategy("projects", user_id=owner_id, name=name).example()
    db.execute(
        f"INSERT INTO projects ({', '.join(row)}) VALUES ({', '.join(['%s'] * len(row))})",
        *row.values(),
    )
    return row
```

Override kwargs accept Hypothesis strategies, callables, or bare values.
Unknown column names raise immediately. Inside a `@given`-decorated test, draw
from the strategy directly instead of calling `.example()`.

Reach for property tests (`check()`, `dataset_strategy`) first — they're
stronger. `row_strategy` is the smaller hammer for the residual case.

## Built for Supabase projects shipping with AI agents

If you're building on Supabase and most of your tests are written by Claude or
Cursor, SqlProof was made for that workflow:

- **[`AGENTS.md`](./AGENTS.md)** — primes your AI coding agent on the exact
  patterns for RLS policies, RPC functions, and stateful tests on a Supabase
  schema.
- **[`sqlproof-skills`](https://github.com/alialavia/sqlproof-skills)** — a
  Claude Code / Cursor plugin that teaches the agent how to drive SqlProof
  end-to-end.
- **[`sqlproof-mcp`](./src/sqlproof/mcp/)** — an MCP server exposing schema
  introspection and property generation as agent tools.
- **[Inbox sample](./examples/inbox/)** — a multi-tenant Supabase app
  (organizations, tickets, agents, messages, pgvector embeddings) with 10
  intentional bugs across RLS, RPCs, and triggers, each paired with a property
  test that catches it and a walkthrough recipe.

**60-second path:** [Test your Supabase project in 60 seconds](https://sqlproof.com/supabase-quickstart/).

## API at a glance

```python
proof = SqlProof.from_schema_file("./schema.sql")
proof = SqlProof.from_connection_string("postgresql://localhost/postgres")

# Property runner (decorator shown above, or method form):
proof.check("name", sizes={"orders": 10}, property=lambda db: ...)

# Shorthand for "this query must return no rows":
proof.invariant(
    "no bad rows",
    sizes={"orders": 10},
    query="SELECT id FROM orders WHERE total < 0",
    expect_empty=True,
)

# Ad-hoc fixture row (see above):
order = proof.row_strategy("orders", customer_id=42).example()

proof.disconnect()
```

The property function receives a `SqlProofClient`:

```python
rows = db.query("SELECT id, total FROM orders WHERE total >= %s", 0)
total = db.scalar("SELECT count(*) FROM orders")
typed = db.query_typed("SELECT id, total FROM orders", OrderRow)
data = db.get_generated_data()
```

Full reference at [sqlproof.com](https://sqlproof.com).

## When a property fails

```text
Property failed: order totals match sum of line items
Failure: AssertionError: expected totals to match
Row context: {'order_id': 1}
Dataset shape: {'orders': {'rows': 1}, 'line_items': {'rows': 2}}
```

Counterexamples are written under `.sqlproof/failures/` and can be inspected
with:

```bash
sqlproof report .sqlproof/failures/test_name.json
sqlproof report .sqlproof/failures/test_name.json --format json
sqlproof replay .sqlproof/failures/test_name.json
```

## Supported PostgreSQL types

`integer`, `smallint`, `bigint`, `serial`, `bigserial`, `numeric(p,s)`, `real`,
`double precision`, `boolean`, `text`, `varchar(n)`, `char(n)`, `uuid`,
`timestamp`, `timestamptz`, `date`, `time`, `json`, `jsonb`, `bytea`,
`vector(n)` (pgvector), custom `ENUM` types, custom domains (with CHECK
inheritance), built-in range types, and composite types.

Schema features parsed and respected: foreign keys, CHECK / UNIQUE / EXCLUSION
constraints, partial unique indexes, `GENERATED ALWAYS AS` columns, and enums.

## How it works

1. **Schema parsing** — reads your SQL file (or introspects a live DB) to extract tables, columns, types, foreign keys, CHECK/UNIQUE/EXCLUSION constraints, partial unique indexes, generated columns, and enums.
2. **Dependency ordering** — topologically sorts tables by foreign key so parents are inserted first.
3. **Data generation** — maps PostgreSQL types to Hypothesis strategies and applies constraint-aware generation for CHECK, UNIQUE, NOT NULL, and FK.
4. **Isolated execution** — schema-file proofs run against an in-memory client for fast local feedback. DSN-backed proofs insert generated data inside savepoints, run the property, then roll back the run.
5. **Shrinking** — when a property fails, Hypothesis shrinks the dataset to find the simplest counterexample.

## Development

```bash
git clone https://github.com/alialavia/sqlproof.git
cd sqlproof
uv sync --extra dev

uv run pytest
uv run ruff check src tests
uv run pyright src/sqlproof
uv run mypy src/sqlproof
```

### Postgres-backed tests

Integration tests are optional and read `SQLPROOF_TEST_DATABASE_URL`:

```bash
SQLPROOF_TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5432/postgres' uv run pytest tests/integration
uv run pytest tests/benchmarks
```

The integration tests create a temporary schema named `sqlproof_it_*` and drop
it at the end.

### Why SqlProof tests itself with properties

SqlProof uses Hypothesis internally, and its own tests use properties for
schema fingerprinting, dependency ordering, FK validity, constraint
generation, shrinking, parser idempotence, and counterexample replay. This
keeps the library honest about the same invariants it asks users to write.

## License

MIT
