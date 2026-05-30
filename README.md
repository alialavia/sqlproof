# SqlProof

[![CI](https://github.com/alialavia/sqlproof/actions/workflows/ci.yml/badge.svg)](https://github.com/alialavia/sqlproof/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/alialavia/sqlproof/branch/main/graph/badge.svg)](https://codecov.io/gh/alialavia/sqlproof)
[![PyPI](https://img.shields.io/pypi/v/sqlproof?include_prereleases&v=2)](https://pypi.org/project/sqlproof/)

**→ Full docs: [sqlproof.com](https://sqlproof.com)**

> ⚠️ **Early-stage alpha (`0.1.0a1`).** APIs are unstable and may change without
> deprecation warnings until 0.x stabilizes. Postgres edge cases and Hypothesis
> shrink behavior are still being discovered, and coverage of the schema surface
> area is incomplete. **Do not rely on this for production test suites yet.**
> Bug reports and reproductions welcome —
> [open an issue](https://github.com/alialavia/sqlproof/issues).
>
> Known gaps tracked openly in the [issue list](https://github.com/alialavia/sqlproof/issues):
> - [#1 CI: real Postgres service container](https://github.com/alialavia/sqlproof/issues/1) — currently the integration suite skips in CI
> - [#2 Coverage: integration-heavy modules](https://github.com/alialavia/sqlproof/issues/2) — `core.py`, `client.py`, `runners/db.py`, `schema/introspect.py` excluded from the coverage gate
> - [#3 Schema: exclusion constraints, partial unique indexes, generated columns](https://github.com/alialavia/sqlproof/issues/3)
> - [#4 Generators: range types, composite types, custom domains](https://github.com/alialavia/sqlproof/issues/4)
> - [#5 Pytest plugin: CLI flags and reporter wiring still stabilizing](https://github.com/alialavia/sqlproof/issues/5)
> - [#6 Deprecation policy for 0.x](https://github.com/alialavia/sqlproof/issues/6)
> - [#7 Coverage: CLI and reporter modules](https://github.com/alialavia/sqlproof/issues/7)

Property-based testing for PostgreSQL schemas and SQL behavior. Define properties about
your database code; SqlProof generates valid datasets with Hypothesis, executes your
queries through `psycopg`, and saves the smallest counterexample it finds.

## Built for Supabase founders who don't write tests by hand

If you're a solo founder building on Supabase and your testing strategy
is "ask Claude / Cursor to write the tests" — SqlProof was made for you.
This repo ships with [`AGENTS.md`](./AGENTS.md), a rules file that
primes your AI coding agent on the exact patterns to use for RLS
policies, RPC functions, and stateful tests on a Supabase schema.

**60-second path:** [Test your Supabase project in 60 seconds](https://sqlproof.com/supabase-quickstart/).

## Install

Alpha releases are gated behind a pre-release flag so you don't get one by accident:

```bash
pip install --pre sqlproof
# or:
uv add --prerelease=allow sqlproof
```

Requires Python 3.11+ and PostgreSQL 13+.

**Running in CI?** See [the CI/CD guide](https://sqlproof.com/guides/ci-cd/)
for copy-paste GitHub Actions workflows covering vanilla Postgres and the
extra setup Supabase-shaped projects need (auth migration, plpgsql_check).

## Quick Start (general)

Given a schema file:

```sql
-- schema.sql
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

Write property tests with pytest:

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

SqlProof will:

1. Parse your schema (tables, columns, FKs, CHECK constraints, enums)
2. Topologically order tables by foreign-key dependencies
3. Generate datasets that respect common type, FK, CHECK, UNIQUE, and NOT NULL constraints
4. Run your property with Hypothesis-managed execution and shrinking
5. Save the shrunk counterexample as JSON when a property fails

## What you can do with SqlProof

### Generate datasets that respect your schema

The generation engine reads your schema and produces multi-table datasets where every FK references a real parent, every CHECK constraint is honored at generation time (no rejection sampling), every UNIQUE constraint is enforced, and types are realistic — `NUMERIC(10,2)` gets scale-2 decimals, `varchar(50)` gets bounded strings, enums sample from declared values.

Useful far beyond tests: seed local dev databases, generate fixtures, replay schema-respecting data through migrations, sample child-row FKs from external parent tables (e.g. Supabase `auth.users`).

→ Walkthrough with column overrides, derived values, shrinkable cardinalities, and external parent tables: [Realistic Data Generation](https://sqlproof.com/examples/data-generation/).

### Property-based testing

A pgTAP test asserts a specific value against a fixed fixture. A SqlProof property describes an *invariant* and lets Hypothesis throw hundreds of valid datasets at it — including edge cases (NULLs, decimal precision, empty groups, tied window values) you wouldn't think to type. When a property fails, Hypothesis shrinks the dataset to the smallest reproducer and saves it.

Common shapes that property tests cover much better than examples:

- **Aggregation invariants** — DB-side aggregate matches a Python recomputation across any input.
- **RLS policy regressions** — every role/membership/sharing combination yields the right visible rows.
- **Migration safety** — old query and new query produce the same answer for every dataset.
- **Idempotency** — operation applied twice = applied once.
- **Round-trip serialization** — JSONB / custom types survive serialize→parse intact.

→ Walkthroughs of all five: [Five Property Patterns](https://sqlproof.com/examples/property-patterns/).

→ The strongest case: testing **SQL functions** with stacked discounts, country-specific tax, and rounding edge cases — pgTAP version side-by-side with the SqlProof version, showing four realistic regressions where pgTAP silently passes and SqlProof catches: [Testing SQL Functions — pgTAP vs SqlProof](https://sqlproof.com/examples/testing-sql-functions/).

→ Honest comparison with pgTAP (where SqlProof wins, where pgTAP wins, where you should ship both): [SqlProof vs pgTAP](https://sqlproof.com/guides/vs-pgtap/).

## API

See the full API reference at [sqlproof.com](https://sqlproof.com).

### Quick reference

```python
proof = SqlProof.from_schema_file("./schema.sql")
proof = SqlProof.from_connection_string("postgresql://localhost/postgres")

proof.check("name", sizes={"orders": 10}, property=lambda db: ...)
proof.invariant(
    "no bad rows",
    sizes={"orders": 10},
    query="SELECT id FROM orders WHERE total < 0",
    expect_empty=True,
)

proof.disconnect()
```

### Schema Sources

**SQL file** — SqlProof parses `CREATE TABLE`, `CREATE TYPE ... AS ENUM`, foreign keys, CHECK constraints, UNIQUE constraints, and column types directly from `.sql` files.

**Connection string** — Pass a `postgresql://` URL and SqlProof introspects the live database via `information_schema` and `pg_catalog`.

```python
proof = SqlProof.from_connection_string("postgresql://localhost:5432/mydb")
```

### Custom Column Generators

SqlProof maps PostgreSQL types to Hypothesis strategies and refines simple range,
`IN (...)`, length, FK, and unique constraints. The public customization API is present
for v0.1 and will grow with richer per-column strategy overrides.

### The `db` Client

The property function receives a `SqlProofClient`:

```python
rows = db.query("SELECT id, total FROM orders WHERE total >= %s", 0)
total = db.scalar("SELECT count(*) FROM orders")
typed = db.query_typed("SELECT id, total FROM orders", OrderRow)
data = db.get_generated_data()
```

- `query()` returns a list of dictionaries.
- `query_typed()` maps rows into `TypedDict`, dataclass, or Pydantic models.
- `get_generated_data()` returns the dataset for the current run.

## Failure Output

When a property fails, SqlProof throws with a formatted counterexample:

```text
Property failed: order totals match sum of line items
Failure: AssertionError: expected totals to match
Row context: {'order_id': 1}
Dataset shape: {'orders': {'rows': 1}, 'line_items': {'rows': 2}}
```

Counterexamples are written under `.sqlproof/failures/` and can be inspected with:

```bash
sqlproof report .sqlproof/failures/test_name.json
sqlproof report .sqlproof/failures/test_name.json --format json
sqlproof replay .sqlproof/failures/test_name.json
```

## How It Works

1. **Schema parsing** — Reads your SQL file (or introspects a live DB) to extract tables, columns, types, foreign keys, CHECK/UNIQUE constraints, and enums

2. **Dependency ordering** — Topologically sorts tables by foreign key references so parent rows are always inserted first

3. **Data generation** — Maps PostgreSQL types to Hypothesis strategies and applies constraint-aware generation for CHECK, UNIQUE, NOT NULL, and FK constraints

4. **Isolated execution** — Schema-file proofs run against an in-memory client for fast local feedback. DSN-backed proofs introspect PostgreSQL, insert generated data inside savepoints, run the property, then roll back the run.

5. **Shrinking** — When a property fails, Hypothesis shrinks the dataset to find the simplest counterexample

## Supported PostgreSQL Types

`integer`, `smallint`, `bigint`, `serial`, `bigserial`, `numeric(p,s)`, `real`, `double precision`, `boolean`, `text`, `varchar(n)`, `char(n)`, `uuid`, `timestamp`, `timestamptz`, `date`, `time`, `json`, `jsonb`, `bytea`, and custom `ENUM` types.

## Development

```bash
git clone https://github.com/your-org/sqlproof.git
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

The integration tests create a temporary schema named `sqlproof_it_*` and drop it at the end.

### Why SqlProof tests itself with properties

SqlProof uses Hypothesis internally, and its own tests use properties for schema
fingerprinting, dependency ordering, FK validity, constraint generation, shrinking,
parser idempotence, and counterexample replay. This keeps the library honest about
the same invariants it asks users to write.

## License

MIT
