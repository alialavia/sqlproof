---
title: Scenarios (experimental)
description: Describe a database state once, pin the rows that matter, and load the same rows every time.
---

:::caution[Experimental]
`sqlproof.scenarios` is new and its API may change in a minor release. It
is not exported from the top-level `sqlproof` package.
:::

A scenario is a named, seeded database state. You pin the rows a case
depends on ("two tier 1 customers, three tier 2, one of them owned by
alice") and the generator fills in everything else from the seed. The
same scenario, seed, schema and sqlproof version load the same rows,
byte for byte, every time.

```python
# scenarios/billing_scenarios.py
from sqlproof.scenarios import scenario


@scenario(seed=42)
def tiered_customers(s):
    alice = s.row("auth.users", name="alice")
    s.row("customers", name="acme", tier=1, owner_id=alice)
    s.table("customers").cohort(2, tier=1).cohort(3, tier=2).fill(20)
    s.table("invoices").per_parent("customers", where={"tier": 1}, count=(5, 10))
```

```bash
sqlproof scenarios list
sqlproof scenarios show tiered_customers --schema-file schema.sql
sqlproof scenarios up tiered_customers --dsn "$DATABASE_URL"
```

## Building a scenario

The function you decorate receives a builder. For each table it records
an ordered list of row groups:

| Call | Rows |
| --- | --- |
| `s.table("t").cohort(n, col=value, ...)` | `n` rows with the listed columns pinned; every other column is generated |
| `s.table("t").fill(n)` | `n` rows with nothing pinned |
| `s.row("t", name="x", col=value, ...)` | one row that other rows can reference as `x` |
| `s.table("t").per_parent("p", where=..., count=..., default=0)` | children sized by their parents, see below |

Table names are bare (`"customers"`) or schema-qualified
(`"billing.customers"`). A bare name that matches tables in two schemas
is an error that asks for the qualified name.

Pinned values are literals, references, or callables. A callable
receives the same context object as a
[per-column override](/examples/data-generation/) and may
return `GENERATE` (from `sqlproof.generators`) to let the generator
choose that row's value after all.

### References

`s.row(...)` returns a reference; `s.ref("name")` makes one for a row
declared anywhere in the scenario. A reference is the row's primary-key
value, so pin it on a foreign-key column:

```python
acme = s.row("workspaces", name="acme", title="Acme")
s.table("projects").cohort(3, workspace_id=acme)
```

Keys are a pure function of the row's position, so references resolve
before anything touches the database. `sqlproof scenarios show` prints
each one.

### Per-parent counts

`per_parent` sizes a child table by its parents. Each parent matching
`where` gets between `count[0]` and `count[1]` children, drawn from the
seed; every other parent gets `default`:

```python
# 5-10 invoices per tier 1 customer, 0-2 for everyone else.
s.table("invoices").per_parent(
    "customers", where={"tier": 1}, count=(5, 10), default=(0, 2)
)

# Ten documents under acme, none anywhere else.
s.table("documents").per_parent("workspaces", where=s.ref("acme"), count=10)
```

`where` is a mapping, a reference, or `None` for every parent. A mapping
matches parent rows whose **cohort pins** those values: a `fill` row that
happens to draw `tier = 1` gets `default`. When the child has more than
one foreign key into the parent, name the one to use with `column=`.

### Supabase users

`auth.users` takes `fill(n)` and named rows. Their ids are derived from
the scenario name, seed and row index, and their emails are
`sqlproof+<scenario>+<index>@test.invalid`. A foreign key into
`auth.users` that you do not pin is assigned one of the scenario's users.

## YAML and JSON scenarios

A scenario does not need Python. Put it in a `*.scenario.yaml` (or
`.yml`, or `*.scenario.json`) file in the scenarios directory, one per
file, and every `sqlproof scenarios` command finds it:

```yaml
# scenarios/tiered_customers.scenario.yaml
# yaml-language-server: $schema=https://sqlproof.com/schemas/scenario-v1.json
version: 1
name: tiered_customers
seed: 42
tables:
  auth.users:
    - row: alice
    - fill: 2
  customers:
    - row: acme
      set: {tier: 1, owner_id: {ref: alice}}
    - cohort: 2
      set: {tier: 1}
    - cohort: 3
      set: {tier: 2}
    - fill: 20
  invoices:
    - per_parent:
        parent: customers
        where: {tier: 1}
        count: [5, 10]
        default: [0, 2]
```

| Python | YAML |
| --- | --- |
| `.cohort(2, tier=1)` | `- cohort: 2` with `set: {tier: 1}` |
| `.fill(20)` | `- fill: 20` |
| `s.row("t", name="acme", tier=1)` | `- row: acme` with `set: {tier: 1}` |
| `s.ref("alice")` | `{ref: alice}` |
| `.per_parent("p", where=..., count=(5, 10))` | `- per_parent: {parent: p, where: ..., count: [5, 10]}` |
| `@scenario(seed=42, clock=...)` | `seed: 42`, `clock: 2026-01-01T00:00:00Z` |

YAML needs PyYAML: `pip install "sqlproof[yaml]"`. JSON files need
nothing extra. A TypeScript project can run the CLI without adding
Python to the project: `uvx --from "sqlproof[yaml]" sqlproof scenarios up
tiered_customers --dsn "$DATABASE_URL"`.

Values are scalars Postgres can read, `{ref: <row name>}`, a list (an
array column) or an object (a `json` or `jsonb` column). An object that
is exactly `{ref: <name>}` is always a reference. Keys starting with
`x-` at the top level are left for other tools and do not change the
scenario.

The JSON Schema at
[`/schemas/scenario-v1.json`](https://sqlproof.com/schemas/scenario-v1.json)
gives editors completion and checks; the comment on the second line of
the example turns it on in VS Code's YAML extension. The CLI checks the
same rules and names the path of anything it rejects, such as
`$.tables['customers'][1].cohort must be an integer`.

### Exporting a Python scenario

```bash
sqlproof scenarios export tiered_customers               # YAML to stdout
sqlproof scenarios export tiered_customers --format json --output tiered.scenario.json
```

An export is the same scenario: same fingerprint, same rows. Dates,
decimals and UUIDs are written as the strings Postgres reads. A scenario
that pins a callable cannot be exported.

## For agents: MCP tools

The `sqlproof-mcp` server has two scenario tools, so an agent can
describe the database state it needs instead of writing seed SQL. Add it
to your MCP client with `uvx --from "sqlproof[mcp]" sqlproof-mcp`. Both
tools take the scenario as a JSON object in the form above:

- `compile_scenario(scenario, schema_sql)` checks the scenario against
  the schema and returns the rows per table and the key of every named
  row. It touches no database.
- `load_scenario(scenario, dsn, schema="public")` loads it. It never
  deletes anything: if a table already holds rows it refuses, and
  emptying the tables is left to a person
  (`sqlproof scenarios up --truncate-existing`). Error messages never
  include the connection string or its password.

## Loading

```python
from scenarios.billing_scenarios import tiered_customers

report = tiered_customers.load(dsn)
report.refs["acme"]   # the primary key of the row named "acme"
report.rows           # rows loaded per table, by qualified name
```

A load is one transaction: either every table holds the scenario's rows,
or nothing changed. It:

1. refuses to run if any modelled table already holds rows, including
   rows that row-level security hides from your role. Pass
   `truncate_existing=True` (`--truncate-existing`) to delete them
   first. **This is destructive.**
2. resets the sequences your tables own, so serial and identity columns
   start from 1;
3. inserts the scenario's `auth.users` rows;
4. bulk-loads every table with `COPY`;
5. moves each sequence past the loaded keys, so your app's own inserts
   afterwards do not collide;
6. runs `ANALYZE`.

### What makes loads identical

Column defaults that would differ between loads are replaced while the
scenario compiles:

| Default | Becomes |
| --- | --- |
| `now()`, `current_timestamp`, `clock_timestamp()`, `current_date`, ... on a timestamp or date column | the scenario clock (`2026-01-01T00:00Z` unless you pass `clock=`) |
| `gen_random_uuid()`, `uuid_generate_v4()` | a UUID derived from the seed |
| `random()` | a float derived from the seed |

A default that calls one of those functions but has some other shape
(`md5(random()::text)`, `now()::text`) is left alone and reported as
`nondeterministic_defaults` on the report and as a warning in the CLI.
Pin those columns if the case depends on them.

Functions that call `now()` at query time still see the real clock.

## In property tests

`scenario.strategy(proof)` is a Hypothesis strategy of datasets that
contain the scenario: every draw has its row counts and every value it
pins, and only the unpinned values vary. A failing property shrinks
those, never what the scenario fixed.

```python
from hypothesis import given


@given(dataset=tiered_customers.strategy(proof))
def test_tier_one_never_pays_more(dataset):
    with proof.client_for_dataset(dataset) as db:
        ...
```

Draws use the same value windows as `load()` (see
[Custom generators](/guides/custom-generators/)), but Hypothesis chooses
the unpinned values, so there is no byte-identity promise here.

## In scale sweeps

Pass a scenario instead of `sizes` to
[`scale_analysis`](/guides/scale-analysis/), and call the function with a
named row:

```python
from sqlproof.scale import scale_analysis
from sqlproof.scenarios import RowRef

result = scale_analysis(
    proof, "invoice_total", scenario=tiered_customers, args=[RowRef("acme")],
)
assert result.exponent < 1.3
```

At factor `k` the scenario grows about `k` times:

- fills grow `k` times;
- cohorts stay fixed (`scale_cohorts=True` grows them too);
- named rows never grow: they are identities;
- per-parent children grow so the total stays linear. A fixed parent
  (a named row, or a cohort) gets `k` times the children; parents that
  themselves multiply keep their per-parent count.

So "acme's invoices as the rest of the data grows" is one sweep. The
scenario's seed is the sweep's, its `auth.users` rows are inserted before
each load and removed at the end, and the run artifact records the
scenario's name and fingerprint.

## From a failing property

A failing property writes its counterexample to
`.sqlproof/failures/<test>.json`, with the seed Hypothesis drew. Turn the
counterexample into a scenario and load it into a database to look at
the rows that broke the property:

```bash
sqlproof replay .sqlproof/failures/test_totals.json --as-scenario \
  --dsn "$DATABASE_URL" --output scenarios/totals_failure.scenario.yaml
sqlproof scenarios up counterexample_test_totals --dsn "$DEV_DATABASE_URL"
```

`--schema-file schema.sql` works in place of `--dsn`; the schema
supplies the keys and references. Each row becomes a pinned row, named
`<table>_<n>` when its table has a single-column key, and every foreign
key to a row in the counterexample becomes a reference to it. The
scenario assigns new keys in the same order, so relationships survive
but key values do not: a property that depends on the numbers
themselves keeps the counterexample file for that. A key that is also a
foreign key (`profiles.id` referencing `auth.users`) is pinned as a
reference, so it keeps its parent. Pass `--name` to choose the scenario's
name.

To repeat the search instead, pass the recorded seed back:
`@sqlproof(proof, sizes=..., seed=<seed>)`; `sqlproof replay FILE`
prints it. A run with no `seed=` is left to Hypothesis, so a
`derandomize` profile or `--hypothesis-seed` still applies (and then the
file records no seed, or the forced one). The seed repeats the random
search, so it finds the failure again only if that search found it: a
failure Hypothesis replayed from its example database was found by an
earlier run, and is reproduced from the counterexample file instead.

## Errors before the database

The scenario is checked against the schema before any row is written:
unknown tables and columns, a pinned generated column, a pinned primary
key that is not made of references (and one that is, unless every row
of the table pins all of it, distinctly),
values that break a `NOT NULL`, enum or `CHECK` constraint, a unique
value pinned on two rows, references to undeclared rows or to the wrong
table, and child rows whose required parent table would be empty.

## Limits in this release

- A table sized by `per_parent` cannot also take cohorts, rows or fills,
  and takes one `per_parent`.
- `auth.users` is the only table outside the loaded schema a scenario
  can seed.
- In Python, `s.row()` takes the row name as `name=`, so a column called
  `name` cannot be pinned on a named row. Pin it with
  `cohort(1, name=...)`, or write the scenario in YAML, where `row:` and
  `set: {name: ...}` do not collide.
- `strategy()` refuses a scenario that seeds `auth.users`: a Hypothesis
  dataset cannot carry rows for a table outside the schema.

## Using `load_dataset` directly

Scenarios are built on `sqlproof.load_dataset`, now public. It bulk-loads
`sizes` rows per table with `COPY`, honouring per-column overrides, and
accepts schema-qualified keys (`{"billing.customers": 100}`) where bare
names are ambiguous.
