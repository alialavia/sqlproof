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

## Errors before the database

The scenario is checked against the schema before any row is written:
unknown tables and columns, pinned primary keys or generated columns,
values that break a `NOT NULL`, enum or `CHECK` constraint, a unique
value pinned on two rows, references to undeclared rows or to the wrong
table, and child rows whose required parent table would be empty.

## Limits in this release

- A table sized by `per_parent` cannot also take cohorts, rows or fills,
  and takes one `per_parent`.
- `auth.users` is the only table outside the loaded schema a scenario
  can seed.
- `s.row()` takes the row name as `name=`, so a column called `name`
  cannot be pinned on a named row. Pin it with `cohort(1, name=...)`.
- Scenarios are Python only. A serializable YAML/JSON form, scenarios as
  Hypothesis strategies, and scenarios as the base of a
  [scale analysis](/guides/scale-analysis/) are planned.

## Using `load_dataset` directly

Scenarios are built on `sqlproof.load_dataset`, now public. It bulk-loads
`sizes` rows per table with `COPY`, honouring per-column overrides, and
accepts schema-qualified keys (`{"billing.customers": 100}`) where bare
names are ambiguous.
