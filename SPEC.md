# SqlProof — Project Specification

## Overview

SqlProof is a Python library for property-based testing of SQL queries, functions, RLS policies, and migrations against PostgreSQL. It introspects the schema, automatically generates valid test data that respects schema constraints (foreign keys, NOT NULL, CHECK, UNIQUE, exclusion, enums), and runs developer-defined properties against the generated data to find counterexamples.

**Core thesis:** Developers define _properties_ — universal invariants about their SQL — and SqlProof generates random valid datasets to try to falsify them. When a property fails, SqlProof reports the minimal counterexample with full reproduction information.

**Built on:** [Hypothesis](https://hypothesis.works) (property-based testing engine), [psycopg](https://www.psycopg.org/psycopg3/) v3 (PostgreSQL client), [testcontainers-python](https://testcontainers-python.readthedocs.io/) (disposable Postgres instances), [pglast](https://github.com/lelit/pglast) (libpg_query bindings for SQL parsing), and [pytest](https://docs.pytest.org/) (test runner integration).

## Package Info

- **Name:** `sqlproof`
- **Language:** Python 3.11+ (3.11, 3.12, 3.13 supported)
- **Package manager:** `uv` / `pip`
- **License:** MIT
- **Database:** PostgreSQL only (v13, v14, v15, v16, v17 supported)
- **Distribution:** PyPI

## Architecture

```
sqlproof/
├── src/sqlproof/
│   ├── __init__.py                 # Public API exports
│   ├── _version.py
│   ├── exceptions.py               # Exception hierarchy (see Errors below)
│   ├── schema/
│   │   ├── __init__.py
│   │   ├── introspect.py           # Live Postgres introspection via pg_catalog
│   │   ├── parse_sql.py            # Parse CREATE TABLE from .sql files via pglast
│   │   ├── model.py                # Internal schema dataclasses
│   │   ├── fingerprint.py          # Canonical schema fingerprint for caching
│   │   └── dependency_graph.py     # FK topological sort
│   ├── generators/
│   │   ├── __init__.py
│   │   ├── columns.py              # Postgres types → Hypothesis strategies
│   │   ├── rows.py                 # Single-table row strategy
│   │   ├── graph.py                # FK-aware multi-table dataset strategy
│   │   ├── constraints.py          # CHECK, UNIQUE, exclusion handling
│   │   ├── functions.py            # Function-call strategies (incl. overload variants)
│   │   └── well_known.py           # urls(), emails(), slugs(), etc.
│   ├── runners/
│   │   ├── __init__.py
│   │   ├── property.py             # @sqlproof: generate → insert → query → check
│   │   ├── stateful.py             # RuleBasedStateMachine wrapper
│   │   ├── migration.py            # Before/after migration snapshot runner
│   │   ├── rls.py                  # Multi-role RLS property runner
│   │   ├── overload.py             # Function-overload runner
│   │   └── db.py                   # Testcontainers + connection lifecycle
│   ├── client.py                   # SqlProofClient: query, scalar, savepoint, etc.
│   ├── coverage/
│   │   ├── __init__.py
│   │   ├── plpgsql.py              # plpgsql_check profiler integration
│   │   ├── schema_shape.py         # Schema-shape coverage tracking
│   │   └── diversity.py            # Generator-diversity reporting
│   ├── reporter/
│   │   ├── __init__.py
│   │   ├── console.py              # Rich-formatted human output
│   │   └── json_io.py              # Machine-readable counterexample format
│   ├── pytest_plugin.py            # pytest plugin (auto-discovered via entry_points)
│   ├── cli.py                      # `sqlproof` command-line tool
│   └── types.py                    # Public TypedDicts, Protocols, type aliases
├── tests/                          # Library's own tests (see Testing the Library)
│   ├── unit/
│   ├── meta/                       # Meta-properties: SqlProof testing itself
│   ├── integration/
│   ├── nocover/                    # High-volume tests, no coverage instrumentation
│   ├── matrix/                     # Postgres-version matrix
│   ├── benchmarks/                 # Performance regression tests
│   └── fixtures/
├── examples/
│   ├── ecommerce/
│   │   ├── schema.sql
│   │   └── test_orders.py
│   └── ripenn_scoring/
│       ├── schema.sql
│       └── test_scoring.py
├── pyproject.toml
└── README.md
```

## Core API

The Python API leans into Python idioms: decorators, context managers, generator-based fixtures, and Hypothesis's strategy composition. Two entry styles are supported: the decorator form (primary) and an imperative form for users who want explicit control.

### Decorator Form (Primary)

```python
from decimal import Decimal
from sqlproof import SqlProof, sqlproof
from hypothesis import strategies as st

# One-time setup (typically in conftest.py)
proof = SqlProof.from_schema_file("./schema.sql")


@sqlproof(proof, sizes={"customers": 20, "orders": 100, "line_items": 500})
def test_order_totals_non_negative(db):
    """Order totals are non-negative."""
    rows = db.query("SELECT total FROM orders")
    assert all(row["total"] >= 0 for row in rows)


@sqlproof(
    proof,
    sizes={"customers": 5, "orders": 5, "products": 5, "line_items": 10},
    runs=50,
)
def test_order_total_matches_line_items(db, check):
    """Stored order total matches sum of line item costs."""
    rows = db.query("""
        SELECT o.id,
               o.total AS stored,
               COALESCE(SUM(li.price * li.quantity), 0) AS computed
        FROM orders o
        LEFT JOIN line_items li ON o.id = li.order_id
        GROUP BY o.id, o.total
    """)
    for r in rows:
        with check.row(order_id=r["id"]):
            assert abs(r["stored"] - r["computed"]) < Decimal("0.01")
```

### Imperative Form

```python
proof = SqlProof.from_schema_file("./schema.sql")

proof.check(
    name="order totals match line items",
    sizes={"customers": 5, "orders": 5, "products": 5, "line_items": 10},
    runs=50,
    property=check_totals_match,
)

# Declarative shorthand: query must return 0 rows
proof.invariant(
    name="no orphan line items",
    sizes={"customers": 10, "orders": 20, "products": 10, "line_items": 50},
    query="""
        SELECT li.id FROM line_items li
        LEFT JOIN orders o ON li.order_id = o.id
        WHERE o.id IS NULL
    """,
    expect_empty=True,
)

proof.disconnect()
```

### Connection Configuration

```python
@dataclass(frozen=True)
class SqlProofConfig:
    connection_string: str | None = None
    schema: str = "public"
    schema_file: str | Path | None = None
    image: str = "postgres:16"
    reuse_container: bool = True
    transaction_per_run: bool = True
    seed: Callable[[SqlProofClient], None] | None = None  # Suite-level setup
    external_tables: Mapping[str, ExternalTableSpec] | None = None
```

Exactly one of `connection_string` or `schema_file` must be provided. Convenience constructors:

```python
SqlProof.from_schema_file("./schema.sql")
SqlProof.from_connection_string(os.environ["DATABASE_URL"])
SqlProof.from_config(SqlProofConfig(...))
```

### External Tables

Use `external_tables` when the schema under test references tables that SqlProof should not
generate or insert, such as Supabase `auth.users`, shared tenant tables, or extension-owned
tables in schemas outside the primary schema.

```python
@dataclass(frozen=True)
class ExternalTableSpec:
    primary_key: str
    sample: Callable[[SqlProofClient], Sequence[object]]
    seed: Callable[[SqlProofClient], None] | Callable[[SqlProofClient, int], None] | None = None
    seed_count: int | SearchStrategy[int] | None = None


from hypothesis import strategies as st
from sqlproof.contrib.supabase import seed_supabase_test_users

proof = SqlProof.from_connection_string(
    os.environ["DATABASE_URL"],
    external_tables={
        "auth.users": ExternalTableSpec(
            primary_key="id",
            seed=seed_supabase_test_users,
            seed_count=st.integers(min_value=1, max_value=20),
            sample=lambda db: [
                row["id"]
                for row in db.query(
                    "SELECT id FROM auth.users WHERE email LIKE 'sqlproof_%@test.invalid'"
                )
            ],
        ),
    },
)
```

External table rows are used as parent rows for generated foreign keys, but they are not
included in the generated dataset and are not inserted by `client_for_dataset()`. If a
generated public table has a foreign key to `auth.users(id)`, SqlProof samples `user_id`
from the values returned by the external table spec. `seed` runs before `sample`, allowing
the test suite to create reusable external parent rows before Hypothesis generates rows
that reference them. If `seed_count` is a Hypothesis strategy, SqlProof draws it inside
`dataset_strategy()`, so Hypothesis can shrink the number of external parent rows needed
to reproduce a failure.

### Dataset Column Overrides

`dataset_strategy()` accepts optional column overrides keyed by `table.column` or
`schema.table.column`. Override values can be constants, Hypothesis strategies, or derived
callbacks.

```python
dataset = data.draw(
    proof.dataset_strategy(
        sizes={
            "projects": 1,
            "brand_prompts": 1,
            "workflow_metadata": 1,
        },
        columns={
            "projects.name": "SqlProof Test Project",
            "brand_prompts.is_active": st.just(True),
            "workflow_metadata.total_checks": lambda ctx: len(
                ctx.rows_by_table["ai_responses"]
            ),
        },
    )
)
```

Derived callbacks receive a `ColumnContext` with the current table, column name, row index,
partial row, rows already generated for the current table, and rows generated for earlier
tables. Derived callbacks can only depend on rows that already exist in insertion order.

### SqlProof Class

```python
class SqlProof:
    @classmethod
    def from_schema_file(cls, path: str | Path, **kwargs) -> "SqlProof": ...

    @classmethod
    def from_connection_string(cls, dsn: str, **kwargs) -> "SqlProof": ...

    @classmethod
    def from_config(cls, config: SqlProofConfig) -> "SqlProof": ...

    def customize(self, table: str, **overrides) -> Self:
        """Register custom Hypothesis strategies or FK distributions per column. Fluent."""

    def dataset_strategy(
        self,
        *,
        sizes: Mapping[str, int | SearchStrategy[int]],
        columns: Mapping[str, object] | None = None,
    ) -> SearchStrategy[Dataset]: ...

    def check(
        self,
        name: str,
        *,
        sizes: dict[str, int],
        property: Callable[..., None],
        setup: Callable[[SqlProofClient], None] | None = None,
        runs: int = 100,
        seed: int | None = None,
        timeout_ms: int = 5000,
        commit: bool = False,
    ) -> None: ...

    def invariant(
        self,
        name: str,
        *,
        sizes: dict[str, int],
        query: str,
        expect_empty: bool = True,
        runs: int = 100,
        seed: int | None = None,
        timeout_ms: int = 5000,
    ) -> None: ...

    def acquire(self, *, persistent: bool = False) -> ContextManager[SqlProofClient]:
        """Acquire a client. With persistent=True, bypasses per-run rollback —
        used for suite-level DDL like installing functions."""

    def disconnect(self) -> None: ...

    def __enter__(self) -> Self: ...
    def __exit__(self, *exc) -> None: ...
```

### SqlProofClient

```python
class SqlProofClient(Protocol):
    def query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        """Execute a query, return rows as dicts. Parameters via %s placeholders."""

    def query_typed(self, sql: str, model: type[T], *params: Any) -> list[T]:
        """Execute and map rows to TypedDict, dataclass, or Pydantic model."""

    def scalar(self, sql: str, *params: Any) -> Any:
        """Execute a query and return the first column of the first row, or None."""

    def execute(self, sql: str, *params: Any) -> int:
        """Execute a non-returning statement, return affected row count."""

    def execute_file(self, path: str | Path) -> None:
        """Execute every statement in a SQL file. Used for suite-level seeding."""

    def savepoint(self) -> ContextManager[None]:
        """Wrap a block in SAVEPOINT/ROLLBACK TO SAVEPOINT, always rolled back."""

    def get_generated_data(self) -> Dataset:
        """Return the generated rows for this run, keyed by table name."""

    @property
    def connection(self) -> psycopg.Connection:
        """Escape hatch for raw psycopg access."""
```

#### `query_typed` Semantics

`model` may be:

- **A `TypedDict`**: rows are returned as `dict` (no instance is constructed; the type is for static checking only). Field presence is checked against `__required_keys__`; extra keys in the row are kept by default.
- **A `dataclass`**: instances are constructed by keyword arguments matching field names. Missing required fields raise `SqlProofMappingError`. Extra columns in the row are dropped.
- **A Pydantic v2 model**: instances are constructed via `model_validate(row_dict)`. Pydantic v1 is not supported. Validation errors propagate as `SqlProofMappingError` wrapping the Pydantic exception.

Detection is by attribute presence: `__pydantic_fields__` (Pydantic), `__dataclass_fields__` (dataclass), or `__required_keys__` / `__optional_keys__` (TypedDict). Ambiguous types raise `SqlProofUsageError` at registration time.

### `check` Object

The `check` parameter is optional in the property signature. If declared, it is injected by the runner and provides per-row context capture for failure reporting.

```python
class Check(Protocol):
    def row(self, **context: Any) -> ContextManager[None]:
        """Run a block in a row context. If the block raises, the context dict is
        attached to the counterexample as `failure.row_context`."""

    def label(self, name: str) -> None:
        """Tag this run with a label for distribution reporting (delegates to hypothesis.event)."""
```

Property functions may declare `(db)` or `(db, check)` — the runner inspects the signature and injects accordingly. Stateful, migration, RLS, and overload runners pass their own additional fixture parameters as documented per runner.

### External Inputs via `@given` Composition

When a property needs inputs that aren't generated from the schema (e.g., arbitrary URL strings for `extract_domain`), users compose Hypothesis's `@given` with `@sqlproof`:

```python
from hypothesis import given, strategies as st
from sqlproof import sqlproof
from sqlproof.strategies import urls

@sqlproof(proof, sizes={"content": 1})
@given(url=st.one_of(st.none(), urls()))
def test_extract_domain_idempotent(db, url):
    once = db.scalar("SELECT extract_domain(%s)", url)
    twice = db.scalar("SELECT extract_domain(%s)", once)
    assert once == twice
```

**Implementation requirement**: the runner must compose the dataset strategy and the external strategies into a single `@composite` strategy passed to one `@given` call internally. Nesting two `@given` calls is incorrect — it produces independent shrinking, which loses cross-strategy minimization. The runner detects an outer `@given` by inspecting the wrapped function's `_hypothesis_internal_use_settings` attribute and merges its strategies into the dataset strategy.

### FK Distribution Strategies

| Strategy              | Behavior                                       | Use case         |
| --------------------- | ---------------------------------------------- | ---------------- |
| `"uniform"` (default) | Equal probability per parent                   | General coverage |
| `"zipf"`              | Skewed: a few parents accumulate most children | Realistic load   |
| `"adversarial"`       | Only first, middle, last parent                | Boundary stress  |
| `"single"`            | All children point to one parent               | Hot-spot testing |

Custom callable signature:

```python
FkDistribution = Callable[[list[Any], "DrawContext"], SearchStrategy[Any]]

def my_distribution(parent_pks: list[Any], ctx: DrawContext) -> SearchStrategy[Any]:
    return st.sampled_from(parent_pks)

proof.customize("orders", fk_distribution={"customer_id": my_distribution})
```

`DrawContext` provides `child_count: int` and `parent_table: str`. Callables must return a strategy, not a value, so values participate in shrinking.

### Pytest Integration

A pytest plugin ships with the package, auto-discovered via entry points. Two surfaces:

1. **The `@sqlproof` decorator** wraps a test with Hypothesis machinery and a database fixture. Each generated dataset triggers one execution.

2. **The `sqlproof_db` fixture** for full pytest control:

```python
import pytest
from sqlproof import SqlProof

@pytest.fixture(scope="module")
def sqlproof_db():
    with SqlProof.from_schema_file("./schema.sql") as p:
        yield p
```

The plugin registers these CLI options:

- `--sqlproof-seed=<int>` — fix the Hypothesis seed for reproducibility
- `--sqlproof-runs=<int>` — override the default `runs` count
- `--sqlproof-show-counterexample` — print full dataset on failure (default: dataset summary)
- `--sqlproof-coverage` — enable plpgsql_check profiler (see Coverage)
- `--sqlproof-diversity-report` — print generator-diversity report at end
- `--sqlproof-postgres-image=<image>` — override testcontainers image
- `--sqlproof-verbose` — set logging level to DEBUG

## Schema

### Input Formats

1. **Live introspection** (preferred): connect to Postgres and query `information_schema` and `pg_catalog`. Reflects the actual deployed schema.

2. **SQL file parsing** (offline fallback): parse `CREATE TABLE`, `CREATE TYPE`, `ALTER TABLE`, `CREATE FUNCTION` from a `.sql` file via `pglast`. Recognized DDL is fully modeled; unrecognized DDL (e.g., `CREATE EXTENSION`, custom operators, `CREATE INDEX USING gin (...)`) is preserved as opaque blocks and replayed verbatim when bootstrapping a test database, but does not contribute to the schema model. Unparseable SQL raises `SqlProofSchemaError` with the offending statement and pglast error pointer; an opt-in `--ignore-unparseable` flag downgrades this to a warning.

### Internal Schema Representation

```python
@dataclass(frozen=True, slots=True)
class PgType:
    kind: Literal["scalar", "array", "enum", "domain", "composite", "range"]
    name: str
    base: "PgType | None" = None
    enum_values: tuple[str, ...] = ()
    array_dim: int = 0

@dataclass(frozen=True, slots=True)
class CheckConstraint:
    expression: str
    parsed: ParsedCheck | None  # None if shape is not recognized

@dataclass(frozen=True, slots=True)
class ParsedCheck:
    kind: Literal["range", "in_set", "regex", "length", "compound"]
    column: str
    payload: Any

@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: PgType
    nullable: bool
    default: str | None
    is_generated: bool
    identity: Literal["always", "by_default"] | None = None

@dataclass(frozen=True, slots=True)
class ForeignKey:
    columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]
    on_delete: Literal["NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"]
    on_update: Literal["NO ACTION", "RESTRICT", "CASCADE", "SET NULL", "SET DEFAULT"]

@dataclass(frozen=True, slots=True)
class Table:
    schema: str
    name: str
    columns: tuple[Column, ...]
    primary_key: tuple[str, ...]
    foreign_keys: tuple[ForeignKey, ...]
    unique_constraints: tuple[tuple[str, ...], ...]
    check_constraints: tuple[CheckConstraint, ...]

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"

@dataclass(frozen=True, slots=True)
class Function:
    schema: str
    name: str
    arg_types: tuple[PgType, ...]
    return_type: PgType
    volatility: Literal["immutable", "stable", "volatile"]
    language: str

@dataclass(frozen=True, slots=True)
class SchemaInfo:
    tables: tuple[Table, ...]
    enums: tuple[PgType, ...]
    functions: tuple[Function, ...]
    domains: tuple[PgType, ...]
```

### Schema Fingerprint

A `SchemaInfo` produces a deterministic fingerprint via `schema.fingerprint.compute(schema_info) -> str` (a hex SHA-256 over canonicalized JSON). Used for caching, container reuse keys, and counterexample artifacts.

### Dependency Graph

Tables are inserted in topological order. Self-referencing tables are handled by ordering self-FK rows after the rest of the table's rows, then assigning self-FK columns in a second pass. Cycles between distinct tables raise `CircularDependencyError`.

## Data Generators

### Column Type Mapping

| PostgreSQL Type                 | Hypothesis Strategy                                          |
| ------------------------------- | ------------------------------------------------------------ |
| `smallint`, `int2`              | `st.integers(-32_768, 32_767)`                               |
| `integer`, `int4`               | `st.integers(-2_147_483_648, 2_147_483_647)`                 |
| `bigint`, `int8`                | `st.integers(-2**63, 2**63 - 1)`                             |
| `serial`, `bigserial`, identity | Skipped (DB-generated)                                       |
| `numeric(p, s)`                 | `st.decimals(min_value, max_value, places=s)`                |
| `real`, `float4`                | `st.floats(width=32, allow_nan=False, allow_infinity=False)` |
| `double precision`, `float8`    | `st.floats(allow_nan=False, allow_infinity=False)`           |
| `boolean`                       | `st.booleans()`                                              |
| `text`                          | `st.text(max_size=255)`                                      |
| `varchar(n)`                    | `st.text(max_size=n)`                                        |
| `char(n)`                       | `st.text(min_size=n, max_size=n)`                            |
| `uuid`                          | `st.uuids()`                                                 |
| `timestamp`, `timestamptz`      | `st.datetimes()` (UTC for timestamptz)                       |
| `date`                          | `st.dates()`                                                 |
| `time`, `timetz`                | `st.times()`                                                 |
| `interval`                      | `st.timedeltas()`                                            |
| `json`, `jsonb`                 | `st.recursive(...)` over JSON value space                    |
| `bytea`                         | `st.binary()`                                                |
| `inet`, `cidr`                  | `st.ip_addresses()`                                          |
| `int4range`, etc.               | Range strategy with bounds-aware composition                 |
| `T[]`                           | `st.lists(strategy_for(T))`                                  |
| Enum                            | `st.sampled_from(enum_values)`                               |
| Domain                          | Strategy of base type, filtered by domain CHECK              |
| Composite                       | `st.builds(...)` over field strategies                       |

### Constraint-Aware Generation

**NOT NULL**: nullable columns wrap with `st.one_of(st.none(), base)` at a configurable null rate (default 10%).

**CHECK constraints**: parsed shapes refine the strategy directly (`> 0`, `IN (...)`, `BETWEEN`, `length() >=`). Unrecognized shapes fall back to **assume + retry** via Hypothesis's `assume()` with a configurable retry budget (default 50). Exhausting the budget logs a warning suggesting a `customize` override and raises `SqlProofGenerationError` if it persists.

**UNIQUE**: single-column uniqueness uses `unique_by`; multi-column uniqueness uses a per-strategy collision tracker that regenerates on conflict.

**EXCLUSION**: handled via assume-and-retry. Dedicated strategies for common shapes are on the v0.2 roadmap.

**Foreign keys**: parents generated first; child FK columns draw from already-generated parent PKs via the configured FK distribution.

### Well-Known Strategies

`sqlproof.strategies` ships:

- `urls(schemes=..., include_path=True, include_query=True, include_fragment=False)`
- `emails(domains=None)` — RFC-5321-valid email strings
- `slugs(min_length=1, max_length=64)` — URL-safe lowercase slugs
- `phone_numbers(country=None)` — E.164 format
- `postal_codes(country)` — country-specific format

### Dataset Strategy Composition

The dataset strategy composes per-table strategies in FK-dependency order using `@composite`. Each table's strategy receives the previously-generated rows so FK columns can sample from real parent keys.

**Shrinking in v0.1**: Hypothesis's general-purpose shrinker operates on the resulting structure (shrinks column values, list lengths, integers). It does **not** know that removing a parent row invalidates child rows. When shrinking produces an invalid FK reference, the runner detects the violation at insert time and treats that shrink attempt as failed (Hypothesis then tries a different shrink), rather than passing an invalid dataset to the property. This is correct but suboptimal — shrunk counterexamples may be larger than necessary.

**FK-aware cascade shrinking is a v0.2 feature.** See Future Scope.

## Property Runner

### Default Execution Model: Transaction-per-Run

For each run, the runner:

1. Acquires a connection from the pool
2. `SAVEPOINT sqlproof_run`
3. Inserts the generated dataset using `COPY FROM` (binary mode)
4. Calls the optional `setup` callback
5. Calls the property function
6. `ROLLBACK TO SAVEPOINT sqlproof_run`
7. Releases the connection

### Known Limitations of Transaction Mode

Transaction-per-run is materially faster than schema-per-run, but the following don't behave correctly under rollback:

- **Sequences** (`nextval`/`currval`): values advance and do not roll back. If your property observes sequence values directly, use `commit=True`.
- **`pg_temp` tables**: persist for the session, not the transaction. Drop them explicitly or use `commit=True`.
- **`LISTEN`/`NOTIFY`**: notifications are sent at commit time. Rolled-back notifications are never delivered.
- **Deferred triggers**: triggers with `INITIALLY DEFERRED` fire at commit. They will not fire under transaction mode.
- **`pg_advisory_lock`**: session-scoped locks persist across rolled-back transactions.

When any of these apply, set `commit=True` on the property:

```python
@sqlproof(proof, sizes={"orders": 5}, commit=True)
def test_deferred_trigger_fires(db):
    ...
```

### Schema-Isolation Mode (`commit=True`)

When `commit=True`:

1. `CREATE SCHEMA sqlproof_<uuid>`
2. `SET search_path TO sqlproof_<uuid>, public`
3. Replay the schema DDL into the new schema
4. Insert generated data
5. Run setup + property
6. `DROP SCHEMA sqlproof_<uuid> CASCADE`

This is slower (~10x) but provides full isolation including for the cases above.

### DBManager

```python
class DBManager:
    def __init__(self, config: SqlProofConfig): ...

    def start(self) -> None:
        """Launch testcontainers (if needed), apply schema, run config.seed if set."""

    @contextmanager
    def acquire(self, *, persistent: bool = False) -> Iterator[SqlProofClient]:
        """persistent=False: rolled-back transaction; persistent=True: bypass rollback."""

    def stop(self) -> None: ...
```

## Capabilities

### Stateful Testing — `@sqlproof.stateful`

Built on Hypothesis's `RuleBasedStateMachine`. Models a sequence of database operations and asserts invariants after each step. Hypothesis's stateful shrinker minimizes the failing rule sequence.

```python
from hypothesis import strategies as st
from hypothesis.stateful import rule, invariant
from sqlproof import sqlproof

@sqlproof.stateful(proof, sizes={"customers": 5, "products": 10})
class OrderLifecycle:
    @rule(customer_id=st.integers(1, 5))
    def create_order(self, db, customer_id):
        order_id = db.scalar(
            "INSERT INTO orders (customer_id) VALUES (%s) RETURNING id",
            customer_id,
        )
        self.last_order_id = order_id

    @rule()
    def cancel_last_order(self, db):
        if hasattr(self, "last_order_id"):
            db.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", self.last_order_id)

    @invariant()
    def cancelled_orders_have_no_shipments(self, db):
        rows = db.query("""
            SELECT 1 FROM orders o JOIN shipments s ON s.order_id = o.id
            WHERE o.status = 'cancelled'
        """)
        assert not rows
```

Stateful tests use `commit=False` by default but switch to `commit=True` automatically when any rule contains DDL or LISTEN/NOTIFY (detected via static inspection of rule bodies at registration).

### Function-Overload Testing — `@sqlproof.function_overloads`

Postgres allows multiple functions with the same name but different signatures. SqlProof tests overload resolution directly:

```python
@sqlproof.function_overloads(proof, function="calculate_value_score")
def test_overload_consistency(db, call_a, call_b):
    """Semantically equivalent calls to different overloads return equivalent results."""
    a = db.scalar(f"SELECT {call_a.sql}")
    b = db.scalar(f"SELECT {call_b.sql}")
    assert a == pytest.approx(b, rel=1e-9)
```

`call_a` and `call_b` are `FunctionCall` objects representing the same logical invocation against different overloads, generated from `pg_proc`. `call.sql` returns a SQL fragment with embedded literals — no parameter binding, since the goal is to exercise the parser's overload resolution.

### Migration Testing — `@sqlproof.migration`

Generates pre-migration data in a fresh schema, snapshots it via `<table>__before` copies, applies the migration, and presents both states to the property.

```python
@sqlproof.migration(
    proof,
    before_schema="./migrations/0042_before.sql",
    migration="./migrations/0043_add_strategy_weights.sql",
    sizes={"prompts": 100, "scores": 500},
)
def test_score_ordering_preserved(db_before, db_after):
    before = [r["id"] for r in db_before.query("SELECT id FROM prompts ORDER BY total_score DESC")]
    after = [r["id"] for r in db_after.query("SELECT id FROM prompts ORDER BY total_score DESC")]
    assert before == after
```

The same generated dataset is materialized once. `db_before` queries against the snapshot tables (`<table>__before`); `db_after` queries against the live (post-migration) tables. Migration runner always uses `commit=True` because migrations contain DDL.

### RLS Property Testing — `@sqlproof.rls`

Generates data and a set of authenticated user contexts, then runs the property as each user. Defaults to **Supabase/PostgREST style** (`SET LOCAL role = 'authenticated'; SET LOCAL request.jwt.claims = '{...}'`); pass `mode="set_role"` for plain `SET ROLE` semantics.

```python
@sqlproof.rls(
    proof,
    sizes={"organizations": 3, "users": 10, "documents": 50},
    roles=["authenticated", "anon"],
    # mode="postgrest" is the default
)
def test_documents_isolated_per_org(db, user, all_data):
    """A user only sees documents in their own organization."""
    visible = db.query("SELECT id, organization_id FROM documents")
    own_org = user["organization_id"]
    assert all(d["organization_id"] == own_org for d in visible)
```

`user` is the authenticated user row; `all_data` is the full dataset (used by the property to compute what _should_ be visible). The runner sets the session for each user, runs the property, and resets between users via `RESET role; RESET request.jwt.claims`.

### Typed Row Access — `query_typed`

```bash
sqlproof generate-types --output schema_types.py
```

```python
from .schema_types import Score

@sqlproof(proof, sizes={"scores": 50})
def test_scores_bounded(db):
    rows = db.query_typed("SELECT id, value FROM scores", model=Score)
    assert all(0 <= r.value <= 1 for r in rows)
```

Codegen produces TypedDicts by default; `--style=dataclass` or `--style=pydantic` for alternatives.

## Coverage

PBT coverage is fundamentally about whether your generators explore the input space, not whether your test code is line-covered. SqlProof provides three coverage signals.

### 1. PL/pgSQL Coverage via `plpgsql_check`

`sqlproof.contrib.plpgsql_coverage` exposes two entry points that wrap the
`plpgsql_check` profiler. Both target PL/pgSQL function bodies only —
`LANGUAGE sql` and other-language functions cannot be profiled by
`plpgsql_check` and are silently filtered out of any candidate list.

**`coverage_session(db, candidates, *, cluster, ...)`** — recommended for
"drive a known cluster of public functions and check each got
non-zero coverage." Handles GUC enablement, language filtering, drift
logging, and skip-on-missing-extension. Yields `(report, installed)`.

```python
from sqlproof.contrib.plpgsql_coverage import (
    assert_nonzero_coverage, coverage_session, drive_in_order,
)

BRAND_RPCS = ["get_brand_visibility_stats", "get_brand_check_history", ...]

def test_brand_rpcs_have_baseline_coverage(proof):
    with proof.client_for_dataset({}) as db:
        with coverage_session(db, BRAND_RPCS, cluster="brand") as (report, installed):
            drive_in_order(installed, drivers, cluster="brand")
    print(report.format())
    assert_nonzero_coverage(report, installed, cluster="brand")
```

**`collect_coverage(db, functions=None, *, schema)`** — low-level
primitive for state-machine-driven scenarios that don't fit the
"iterate a drivers dict" shape:

```python
from sqlproof.contrib.plpgsql_coverage import collect_coverage

with collect_coverage(db, functions=["my_func"]) as report:
    proof.run_state_machine(MyMachine, ...)
print(report.format())
```

The console report:

```
PL/pgSQL coverage: 1/2 functions fully covered

get_user_usage_total  stmt 75%  branch 50%  (6/8 executable lines)
  ------------------------------------------------------------
  >    1    1  BEGIN
  >    2    1    SELECT ...
       5         IF total < 0 THEN
       6           RAISE EXCEPTION '...'

get_geo_performance  stmt 100%  branch 100%  (42/42 executable lines)
```

The extension must be installed in the target database
(`CREATE EXTENSION IF NOT EXISTS plpgsql_check`). `coverage_session`
calls `pytest.skip` when it isn't; `collect_coverage` raises
`PlpgsqlCheckNotAvailable` directly.

### 2. Schema-Shape Coverage

For each property run, SqlProof records which schema-shape categories the generated dataset exercised:

- Empty / single-row / many-row variants per table
- Each enum variant
- Each nullable column's null and non-null branches
- Each CHECK constraint's boundary values
- Each FK distribution

At suite end, an aggregate report shows which shapes were _never_ generated:

```
Schema coverage gaps:
  user_usage.feature enum 'export' — never generated (4 properties affected)
  geo_citations.engine enum 'gemini' — never generated (2 properties affected)
  orders rows: zero rows variant — never tested
```

### 3. Generator Diversity

For each property, the runner records a canonicalized fingerprint of the generated dataset (sorted, normalized) and reports `distinct_datasets / total_runs`:

```
Generator diversity:
  test_total_monotone: 14/100 distinct (14%) — consider broader strategies
  test_engine_sums_match_overall: 87/100 distinct (87%) — good diversity
```

Users can also use Hypothesis's native `event()` and `target()` inside properties; these are reported alongside SqlProof's metrics.

## Reporter

### Console Output

Failure output uses [`rich`](https://github.com/Textualize/rich), degrading gracefully in non-TTY environments:

```
✗ Property failed: order totals match line items
  After 23 runs · seed: 1708891234 · shrunk 4 times

  Row context: {order_id: 1}

  Counterexample:
    customers: [{id: 1, name: ''}]
    orders: [{id: 1, customer_id: 1, total: 100.00}]
    line_items:
      - {id: 1, order_id: 1, price: 30.00, quantity: 2}
      - {id: 2, order_id: 1, price: 50.00, quantity: 1}

  Failing assertion:
    abs(stored - computed) < Decimal('0.01')
    where stored=Decimal('100.00'), computed=Decimal('110.00')

  Reproduce:
    pytest tests/test_orders.py::test_order_total_matches_line_items \
      --sqlproof-seed=1708891234

  Full counterexample written to: .sqlproof/failures/test_order_total_matches_line_items.json
```

### JSON Counterexample Format

Counterexamples are written to `.sqlproof/failures/<test_name>.json`:

```json
{
  "$schema": "https://sqlproof.dev/schemas/counterexample-v1.json",
  "version": 1,
  "property_name": "order totals match line items",
  "test_id": "tests/test_orders.py::test_order_total_matches_line_items",
  "seed": 1708891234,
  "shrink_steps": 4,
  "runs": 23,
  "schema_fingerprint": "sha256:abc...",
  "row_context": { "order_id": 1 },
  "dataset": {
    "customers": [{ "id": 1, "name": "" }],
    "orders": [{ "id": 1, "customer_id": 1, "total": "100.00" }],
    "line_items": [
      { "id": 1, "order_id": 1, "price": "30.00", "quantity": 2 },
      { "id": 2, "order_id": 1, "price": "50.00", "quantity": 1 }
    ]
  },
  "failure": {
    "kind": "assertion",
    "message": "abs(stored - computed) < Decimal('0.01')",
    "locals": { "stored": "100.00", "computed": "110.00" },
    "traceback": ["..."]
  }
}
```

The JSON Schema is published at the `$schema` URL and shipped at `src/sqlproof/schemas/counterexample-v1.json`. All Decimal values serialize as quoted strings to preserve precision. Datetimes serialize as ISO 8601 with timezone. UUIDs serialize as lowercase strings.

## Errors

```python
class SqlProofError(Exception):
    """Base for all SqlProof errors."""

class SqlProofUsageError(SqlProofError):
    """Caller misuse: invalid sizes, conflicting decorators, ambiguous types, etc."""

class SqlProofSchemaError(SqlProofError):
    """Schema parsing or introspection failure."""

class CircularDependencyError(SqlProofSchemaError):
    """FK cycle between distinct tables."""

class SqlProofGenerationError(SqlProofError):
    """Data generation exhausted retry budget for assume-and-retry constraints."""

class SqlProofMappingError(SqlProofError):
    """query_typed could not map a row to the requested model."""

class SqlProofTimeoutError(SqlProofError):
    """A property run exceeded its timeout."""

class SqlProofPropertyFailure(SqlProofError):
    """The property was falsified. Carries the counterexample as `.counterexample`."""

class SqlProofContainerError(SqlProofError):
    """testcontainers startup, container died mid-run, etc."""
```

`SqlProofPropertyFailure` is the only exception users typically catch in test bodies. Test runners catch `SqlProofError` at the top level.

## Concurrency Model

- **Threading**: a `SqlProof` instance is **not thread-safe**. Within one process, properties run serially. Hypothesis itself is single-threaded per test.
- **pytest-xdist**: each xdist worker is a separate process with its own `SqlProof` instance and its own testcontainer. To avoid container thrash, set `reuse_container=True` (the default); the container name is derived from the schema fingerprint so workers share the underlying Docker container.
- **Connection pool**: each `SqlProof` instance owns a small psycopg connection pool (default size: 4). Properties acquire and release per-run.

## Determinism Contract

Given the same `seed`, the following are reproducible:

- Generated dataset values (column values, FK choices, row counts within their bounds)
- Order of property executions within a single property's runs
- Shrinking trace

The following are **not** controlled by the seed and may vary:

- Postgres-side defaults (`NOW()`, `gen_random_uuid()`, `nextval`) unless explicitly overridden
- Wall-clock-dependent observations (`pg_stat_*`, etc.)
- Container startup time

Users who need fully deterministic Postgres-side defaults should override them in their schema or set them in property `setup`.

## Resource Cleanup

- `SqlProof` is a context manager and **must** be used as one in production code. Direct `__init__` calls without `__exit__` may leak containers.
- An `atexit` handler is registered on first container startup to stop all SqlProof-managed containers on interpreter exit. This handles `SystemExit` cleanly but not `os._exit` or SIGKILL.
- `SIGINT` (Ctrl-C) raises `KeyboardInterrupt`; `__exit__` runs and stops the container. SIGTERM is handled identically via a registered signal handler.
- Containers are tagged with the SqlProof process PID; `sqlproof clean-orphans` removes containers whose owning process is gone.

## CLI

```
sqlproof generate-types [--connection-string DSN | --schema-file PATH]
                        [--output FILE] [--style typeddict|dataclass|pydantic]
                        [--schema NAME]
    Generate Python type definitions from the live or file-based schema.

sqlproof introspect [--connection-string DSN] [--schema NAME] [--format json|text]
    Print the introspected schema. Useful for debugging generator behavior.

sqlproof run TEST_PATH [pytest-options]
    Thin wrapper around `pytest TEST_PATH` that injects --sqlproof-* defaults.

sqlproof replay COUNTEREXAMPLE_JSON
    Re-run a property using the saved counterexample. Verifies the bug still reproduces.

sqlproof clean-orphans
    Remove orphaned testcontainers from previous runs.

sqlproof version
    Print version and dependency versions.
```

## Performance Targets

These are SLOs the implementation should hit; CI fails if regression exceeds 20%.

- Schema introspection (50-table schema): < 500 ms
- Dataset generation + insert (1000 rows total across 5 tables): < 200 ms in transaction mode
- Per-run overhead (excluding property execution): < 50 ms in transaction mode, < 500 ms in schema mode
- Testcontainer startup (cold, with `reuse_container=False`): < 8 s for `postgres:16`
- Testcontainer reuse (with `reuse_container=True`): < 100 ms

## Logging

Uses the standard `logging` module under the `sqlproof` namespace. Default level is WARNING. Sub-loggers:

- `sqlproof.runner` — per-run lifecycle
- `sqlproof.generator` — strategy composition, retry-budget warnings
- `sqlproof.schema` — introspection, parsing
- `sqlproof.db` — testcontainers, connection pool
- `sqlproof.coverage` — plpgsql_check integration

`--sqlproof-verbose` on pytest sets the level to DEBUG. The CLI accepts `-v`.

## Dependencies

```toml
[project]
name = "sqlproof"
requires-python = ">=3.11"
dependencies = [
  "hypothesis>=6.100",
  "psycopg[binary]>=3.1",
  "pglast>=6.0",
  "rich>=13.0",
]

[project.optional-dependencies]
testcontainers = ["testcontainers[postgres]>=4.0"]
pydantic = ["pydantic>=2.0"]
dev = [
  "pytest>=8.0",
  "pytest-xdist",
  "pytest-benchmark",
  "syrupy",
  "mutmut",
  "mypy",
  "pyright",
  "ruff",
  "uv",
]

[project.entry-points.pytest11]
sqlproof = "sqlproof.pytest_plugin"

[project.scripts]
sqlproof = "sqlproof.cli:main"
```

## Build

- **Build:** `uv build` (PEP 517 via `hatchling`)
- **Lint/format:** `ruff check`, `ruff format`
- **Type check:** `pyright` (strict mode); `mypy` for cross-validation
- **Test:** `pytest` (see Testing the Library below)

## Testing the Library

SqlProof is a foundational testing library. It must hold itself to a higher standard than the code that uses it. The testing approach is modeled on Hypothesis's own self-testing strategy, with adaptations for the SQL/database domain.

### Test Suite Structure

The `tests/` directory is split by purpose:

- **`tests/unit/`** — fast unit tests with no database. Test the schema model, dependency graph, type mappings, exception hierarchy, JSON serialization. Target: < 5 seconds total.
- **`tests/meta/`** — meta-property tests where SqlProof tests itself using Hypothesis. See "Meta-Properties" below.
- **`tests/integration/`** — full integration tests against a real Postgres. Run on every commit. Target: < 60 seconds.
- **`tests/nocover/`** — high-volume runs (1000+ Hypothesis examples per test) without coverage instrumentation. Catch rare bugs. Run nightly, not on every commit.
- **`tests/matrix/`** — Postgres version compatibility tests across PG 13/14/15/16/17.
- **`tests/benchmarks/`** — performance regression tests using `pytest-benchmark`. Compared against baseline; fail on > 20% regression.

### Meta-Properties

The library uses Hypothesis to test its own correctness. These are properties about properties — invariants that must hold for any user-defined property and any schema.

**Required meta-properties for v0.1:**

1. **Generated values satisfy claimed types.** For every Postgres type in the mapping table, the generator's output, when inserted via psycopg, must succeed. Generate a single-column table of each type, generate rows, insert, assert no failures.

2. **Generated datasets satisfy all schema constraints.** For arbitrary schemas (generated by a meta-strategy that produces valid CREATE TABLE statements), the generated dataset inserts without violating any constraint. If insertion fails, that's a generator bug.

3. **FK references are always valid.** For any schema with FKs and any size config, no child row's FK column points to a nonexistent parent PK.

4. **Shrinking is monotone.** If shrinking returns dataset D' from D, then D' is "smaller than or equal to" D under a defined ordering (fewer rows, lexicographically smaller values). This is a property about the framework's behavior, not a user property.

5. **Determinism: same seed → same dataset.** Run the dataset generator twice with the same seed; assert byte-equal output.

6. **Idempotence of introspection.** Introspecting the same schema twice produces equal `SchemaInfo`. JSON round-trip preserves equality.

7. **Schema parser ↔ introspection agreement.** For schemas expressible in both forms, parsing the SQL file and introspecting the resulting database produce equivalent (modulo introspection-only fields) `SchemaInfo`.

   **Known divergences allowlist.** Not all DDL constructs survive the parse → apply → introspect round-trip cleanly. The implementation maintains an allowlist of acceptable divergences in `tests/meta/round_trip_allowlist.py` covering at minimum: column-default expression normalization (e.g., `0` vs `'0'::integer`), constraint name auto-generation when the user didn't specify one, type name canonicalization (`int4` vs `integer`), and whitespace inside CHECK expressions. The meta-property compares schemas with these divergences masked out. New divergences discovered during implementation are added to the allowlist with a comment explaining why they're acceptable; divergences that represent actual bugs are fixed rather than allowlisted.

8. **Counterexample replay reproduces failure.** For any failing property, running `sqlproof replay <counterexample.json>` reproduces the same assertion failure with the same row context.

Each meta-property is implemented as a Hypothesis test in `tests/meta/`:

```python
# tests/meta/test_generators_satisfy_constraints.py
from hypothesis import given, settings
from sqlproof.testing import schemas, datasets_for

@given(schema=schemas(max_tables=5, max_columns=10))
@settings(max_examples=200, deadline=None)
def test_generated_dataset_satisfies_constraints(schema, postgres):
    """Any dataset generated for any valid schema inserts without constraint violations."""
    dataset = datasets_for(schema, sizes={t.name: 5 for t in schema.tables}).example()
    with postgres.fresh_database() as db:
        db.apply_schema(schema)
        # Should not raise:
        db.bulk_insert(dataset)
```

The `sqlproof.testing` submodule exposes meta-strategies (`schemas()`, `tables()`, `columns()`, `datasets_for()`) for these tests. This submodule is also useful for downstream users writing their own tooling around SqlProof.

**Scope of `sqlproof.testing` for v0.1**: ship a deliberately minimal version that covers the common case. The strategy generates schemas with:

- Scalar column types only: `integer`, `bigint`, `text`, `varchar(n)`, `boolean`, `uuid`, `timestamptz`, `numeric(p,s)`, native enums
- NOT NULL and simple CHECK constraints (range, IN-set, length)
- Single-column primary keys
- Single-column foreign keys with `ON DELETE NO ACTION`
- Single-column UNIQUE constraints
- Up to N tables with at most M columns each (configurable; defaults are small enough that meta-tests run in seconds)

Out of v0.1 scope (defer to v0.2 if needed): composite types, ranges, arrays, domains, exclusion constraints, multi-column PKs/FKs/UNIQUEs, generated columns, identity columns, `ON DELETE CASCADE/SET NULL/etc.`, complex CHECK expressions, and self-referencing FKs. Meta-properties that would require these features should be marked `@pytest.mark.skip(reason="requires v0.2 schema strategy")` and tracked as known gaps.

The minimal version is sufficient to validate meta-properties 1, 3, 4, 5, 6, and 8. Meta-property 2 is partially validated (within the scoped feature set). Meta-property 7 has special handling (see below).

### Fuzz Targets

The schema parser is fuzzed against random and adversarial inputs:

- **Random ASCII** — should never crash; either produces valid `SchemaInfo` or raises `SqlProofSchemaError`
- **Real-world schemas** — a corpus of open-source Postgres schemas (Supabase examples, dbt-utils, popular OSS apps) parsed in CI on every commit
- **pgsql-parser test corpus** — re-uses pglast's own test inputs as a baseline

Fuzz tests live in `tests/integration/fuzz/` and run for a fixed time budget per commit (default: 60 seconds), with discovered crashes saved to a regression corpus.

### Snapshot Tests

For shrinking output stability, the library uses snapshot tests via [`syrupy`](https://github.com/syrupy-project/syrupy). For a fixed seed and a known-failing property, the shrunk counterexample is asserted to match a saved snapshot. Changes to shrinker logic that alter shrunk output (even if still correct) require an explicit snapshot update via `pytest --snapshot-update`. This catches regressions in shrinker quality.

### Coverage Targets

- **Line coverage**: > 95% for `src/sqlproof/` (excluding `cli.py` and `pytest_plugin.py`, which are integration-tested)
- **Branch coverage**: > 90%
- **Meta-property pass rate**: 100% (any meta-property failure is a release-blocker)
- **Mutation testing**: `mutmut` run nightly; surviving mutants tracked as bugs

Coverage is enforced in CI via `pytest --cov=sqlproof --cov-fail-under=95`. The `tests/nocover/` directory is excluded from coverage measurement (Hypothesis instrumentation skews numbers).

### Release Tests

Tagging a release runs an extended suite (`pytest -m release`):

- Full Postgres version matrix (13/14/15/16/17) on Linux and macOS
- 10x normal Hypothesis examples for every meta-property
- Benchmark suite with stricter thresholds (5% regression instead of 20%)
- Documentation build and link check
- Example projects (`examples/ecommerce/`, `examples/ripenn_scoring/`) installed as a downstream user would and exercised end-to-end

The release suite must pass before publishing to PyPI.

### CI Configuration

GitHub Actions workflows:

- `ci.yml` — runs on every PR. Unit, integration, meta, lint, type-check.
- `nightly.yml` — runs daily at 02:00 UTC. nocover, fuzz, benchmark, mutation.
- `release.yml` — runs on tag push. Release tests, build, publish.

## Future Scope (post v0.1)

- **FK-aware dataset shrinking** (v0.2): cascade-aware removal of orphaned children when shrinking parents. v0.1 relies on Hypothesis's general-purpose shrinker plus invalid-shrink rejection at insert time.
- **Exclusion-constraint native strategies** (v0.2): non-overlapping ranges per group, geometric exclusions.
- **LLM-suggested properties** (v0.2): point SqlProof at a schema + queries; LLM proposes properties to test.
- **CI/CD reporting** (v0.2): JUnit XML, GitHub Actions annotations, dashboard JSON.
- **Workflow testing** (v0.3): Temporal/trigger.dev property tests.
- **Multi-database support** (v0.3): MySQL, SQLite. Lower priority — Postgres-first.
- **Formal verification mode** (v1.0): SMT-solver (Z3) backed verification for critical properties.
- **Web dashboard / hosted runner**: separate product, talks to OSS lib via the JSON I/O contract.
- **VS Code extension**: in-editor property authoring and result navigation.

## Definition of Done — v0.1

The implementation is considered complete when **all** of the following are true. This is a self-verifiable checklist; the agent should not declare v0.1 complete until each item passes.

1. **E-commerce example passes.** All property tests in `examples/ecommerce/test_orders.py` pass against the e-commerce schema with default settings.
2. **Ripenn example passes.** The scoring-function property test in `examples/ripenn_scoring/test_scoring.py` passes against the bundled schema.
3. **All eight meta-properties pass.** The tests in `tests/meta/` pass on at least Postgres 16. Properties that require v0.2 schema-strategy features may be skipped with explicit reasons; meta-properties 1, 3, 4, 5, 6, 7 (with allowlist), and 8 must pass without skips.
4. **Coverage thresholds met.** `pytest --cov=sqlproof --cov-fail-under=95` passes. Branch coverage above 90%.
5. **Performance targets met.** All SLOs in the Performance Targets section pass on a reference machine (CI runner is acceptable as the reference). Benchmark suite in `tests/benchmarks/` runs and produces a baseline.
6. **`sqlproof replay` round-trip works.** Take a known-failing property, capture its counterexample JSON, then run `sqlproof replay <file>` and verify the same failure reproduces with the same row context.
7. **CLI subcommands all functional.** Each subcommand listed in the CLI section runs without crashing on a representative input. `--help` produces non-empty output for each.
8. **Pytest plugin auto-discovered.** Installing the package and running `pytest --help` shows the `--sqlproof-*` options without any explicit configuration.
9. **All five capability runners work end-to-end.** A passing test exists for each of: `@sqlproof` (basic), `@sqlproof.stateful`, `@sqlproof.migration`, `@sqlproof.rls`, `@sqlproof.function_overloads`.
10. **Type checking passes.** `pyright --strict src/sqlproof/` passes with zero errors. `mypy src/sqlproof/` passes with zero errors.
11. **Lint passes.** `ruff check src/ tests/` passes with zero errors.
12. **Documentation builds.** The README's quick-start example, copy-pasted as written, runs and passes against a fresh Postgres.

Items not on this list — including FK-aware cascade shrinking, exclusion-constraint native strategies, and the ambitious end of `sqlproof.testing` — are explicitly v0.2 or later.

## Implementation Priority (Weekend)

### Saturday — Core engine

1. Project scaffolding (`pyproject.toml`, `src/` layout, pytest config, pyright strict)
2. Exception hierarchy
3. Schema introspection via `pg_catalog`; SQL file parsing via `pglast`
4. Schema fingerprint
5. Dependency graph + topological sort
6. Column type → Hypothesis strategy mapping
7. Constraint handler (NOT NULL, CHECK, UNIQUE, FK) with assume-and-retry
8. Dataset strategy composition
9. DBManager: testcontainers + transaction-per-run + commit-mode
10. SqlProofClient: query, query_typed, scalar, execute, execute_file, savepoint
11. `@sqlproof` decorator + `check.row` context capture
12. Reporter (console + JSON)
13. End-to-end: e-commerce example passes

### Sunday — Capabilities + library testing

14. Stateful runner via `RuleBasedStateMachine`
15. Migration runner with snapshot tables
16. RLS runner with PostgREST-style and SET ROLE modes
17. Function-overload generator and runner
18. Well-known strategies (`urls`, `emails`, etc.)
19. `query_typed` + `sqlproof generate-types`
20. Pytest plugin with all CLI flags
21. Coverage: plpgsql_check integration, schema-shape, diversity reporting
22. Library self-tests: meta-properties 1–8 in `tests/meta/`
23. CLI subcommands (`run`, `replay`, `introspect`, `generate-types`, `clean-orphans`, `version`)
24. Ripenn example: scoring-function property test
25. README with quick start
