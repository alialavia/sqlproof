---
title: Scale Analysis (experimental)
description: Measure how a SQL function's work grows with data, and fail CI when it grows faster than you allow.
---

:::caution[Experimental]
`sqlproof.scale` is new in 0.12 and its API may change in a minor
release. It is not exported from the top-level `sqlproof` package. Read
[What it cannot see](#what-it-cannot-see) before relying on it as a CI
gate.
:::

A query that is fine on 1,000 rows can fall over at 1,000,000.
`scale_analysis` loads your schema at a ladder of sizes, runs a function
at each one, and fits how its work grows. The answer is an exponent: about
1 is linear, about 2 is quadratic. You assert on it like any other
property:

```python
from sqlproof import SqlProof
from sqlproof.config import SqlProofConfig
from sqlproof.scale import heaviest, scale_analysis

proof = SqlProof.from_config(
    SqlProofConfig(connection_string=TEST_DSN, schema="billing")
)


def test_compute_invoice_survives_growth():
    result = scale_analysis(
        proof,
        "billing.compute_invoice",
        sizes={"customers": 100, "invoices": 1000, "line_items": 5000},
        args=[heaviest("billing.customers.id")],
    )
    assert result.exponent < 1.5
    assert not result.spills_below(50_000)
```

:::danger[Destructive: use a dedicated test database]
The sweep **empties and repopulates every table** in the proof's schema,
and leaves them empty when it finishes. It refuses to start while any of
those tables holds rows, or while row-level security hides rows from its
role, unless you pass `truncate_existing=True`.

To leave your tables alone whatever they hold, pass `in_clone=True`: the
sweep then runs in a throwaway copy of the database (`CREATE DATABASE ...
TEMPLATE`), dropped when it ends. That needs the CREATEDB privilege and
no other sessions on the database while it is copied; behind Supavisor,
add `release_template_sessions=True`, as for mutation testing.
:::

## Why an exponent, and why buffers

A complexity class belongs to the function, not the machine. `O(n²)` is
true on a laptop, in CI and in production; "times out at 380K rows"
changes the day someone upgrades the runner.

The exponent is fitted on **buffer counts** (pages read or hit, from
`EXPLAIN (ANALYZE, BUFFERS)`), not wall-clock time. On an idle machine,
wall-clock time varied 1.96× between identical runs; buffer counts varied
1.0007×. That is what makes `assert result.exponent < 1.5` stable on a
noisy CI runner.

## How a sweep runs

1. **Validate** everything before touching a table: sizes, function
   name, that the bulk loader can load the schema.
2. **Calibrate** the fixed per-call cost on a one-row load, so it is not
   mistaken for growth.
3. **Climb a geometric ladder** (×1, ×2, ×4, …): truncate, bulk load with
   `COPY`, `ANALYZE`, resolve the arguments against the new data, probe.
4. **Stop** when at least `min_points` points fit well enough to claim
   an exponent (its standard error at most 0.15), at
   `max_factor`, or when a probe exceeds `probe_timeout_s`. Postgres
   cancels such a probe at the timeout, and the result is marked
   truncated.
5. **Split the fit** wherever the plan's shape changes, and report each
   regime.

Options passed through to the sweep: `max_factor` (default 32),
`min_points` (default 5), `probe_timeout_s` (default 30), `seed`
(default 0), `columns` (per-column overrides, as for bulk generation) and
`distribution`. With `distribution="zipf"`, a few parents own most of
the children, as real tenants do; uniform data (the default) spreads
them evenly. Pair it with `heaviest` to measure the heavy tenant.

## Choosing arguments

Functions that take a key need one that exists at every scale. Argument
resolvers are re-run against each freshly loaded dataset:

| Resolver | Picks |
| --- | --- |
| `heaviest("t.col")` | The most-referenced key: the most rows pointing at it, over every foreign key into the column. The largest key when nothing references it. Heaviest by row count, not a worst-case guarantee. |
| `median_key("t.col")` | The middle key in key order: a typical value. |
| `random_key("t.col", seed=0)` | An arbitrary key, deterministic for a seed. |

Plain values are passed through unchanged.

To measure one particular row as everything else grows, sweep a
[scenario](/guides/scenarios/#in-scale-sweeps) and pass `RowRef("name")`:
the named row's key, the same at every factor.

## Reading the result

| Member | Meaning |
| --- | --- |
| `exponent` | Exponent of the final plan regime. Raises `SqlProofScaleError`, with the reason, when the fit is inconclusive or execution time grows clearly faster than the work (see below). |
| `r_squared` | Fit quality of the final regime, for reading. Acceptance uses the exponent's standard error (`regimes[-1].slope_stderr`): R² of a flat series measures only its noise. |
| `regimes`, `plan_flips` | Each plan regime's fit, and where the plan changed. |
| `spills_below(rows)` | Whether a sort or hash spilled to disk at or below `rows`. Raises beyond the measured range rather than guess. |
| `rows_before_timeout(ms)` | Projected row band where the function crosses a timeout. Machine-dependent, unlike `exponent`. |
| `points` | Every probe: factor, rows, work and temp blocks, peak memory, plan hash, time, arguments. |

Each run also writes a JSON artifact to `.sqlproof/scale-runs/` (pass
`artifact_dir=None` to skip). It uses the same envelope as
[mutation runs](/guides/mutation-testing/), so you can keep a history per
function.

To read the history, render a report over those runs:

```bash
sqlproof scale report                 # writes scale-report.html
sqlproof scale report --format text   # one line per function, for CI logs
```

Per function, it shows the exponent over time, with each run's standard
error, the rows measured, any spill, plan flips and the data
distribution. A run whose exponent is more than 0.3 above the
function's previous one is flagged `REGRESSED`. A refused exponent shows
as refused, with the reason, by the same rules `exponent` applies in a
test.

## Index suggestions

Pass `advisors=True` to ask Supabase's
[`index_advisor`](https://github.com/supabase/index_advisor) which
indexes would help. It is called once, after the sweep, on the largest
dataset it measured, and plans each statement with hypothetical indexes
(through `hypopg`). Nothing is created and nothing extra runs.

```python
result = scale_analysis(
    proof,
    "public.invoice_count",
    sizes={"customers": 20, "invoices": 1000},
    args=[heaviest("public.customers.id")],
    nested_plans=True,
    advisors=True,
)
result.advisor_hits[0].index_statement
# 'CREATE INDEX ON public.invoices USING btree (customer_id)'
```

`result.advisor_hits` lists the suggestions, highest estimated saving
first. Each hit has `rank`, `relation`, `index_statement`, the
`statement` it came from, and the planner's estimated
`startup_cost_before`/`_after` and `total_cost_before`/`_after` for that
statement. Only indexes on tables the sweep scaled are kept. The same
list goes into the run artifact as `advisor_hits`, and
`sqlproof scale report` shows the latest run's suggestions.

Which statements are advised:

- With `nested_plans=True`, every statement the function ran that reads
  a table, as `auto_explain` reported it. Parameter names in those
  statements become typed generic parameters (`customer_id = c` becomes
  `customer_id = ($1::bigint)`).
- Always, the outer `SELECT fn($1, ...)` call. Without `nested_plans`,
  that is the only statement, and `index_advisor` sees inside the
  function only if Postgres inlines it. A PL/pgSQL function, or a SQL
  function with a `FROM` clause, is never inlined in a select list.
  Pass `nested_plans=True` for those.

SqlProof never installs the extension. If it is missing,
`result.advisor_notes` says so
(`index_advisor not installed (CREATE EXTENSION index_advisor CASCADE)`)
and the measurement still stands. The notes also list statements
`index_advisor` could not plan, such as PL/pgSQL expressions that use
local variables. Install it once in your test database:

```sql
CREATE EXTENSION index_advisor CASCADE;  -- also installs hypopg
```

With `in_clone=True`, the throwaway copy inherits the extension from the
database it copies. Supabase's other advisor, splinter, is not wrapped
yet.

## What it cannot see

- **Growth in CPU work alone.** Buffers count pages touched, not CPU. A
  join on `a.v < b.v` over a materialized inner side is quadratic in CPU
  but its work fits about 1. When execution time grows more than 0.5
  faster than the work (and the last call took at least 20 ms),
  `exponent` refuses instead of reporting the smaller number. Below
  that, CPU growth passes unseen.
- **Plans inside a function, by default.** `EXPLAIN` of `SELECT fn()`
  shows only the outer statement. For PL/pgSQL and non-inlined SQL
  functions, work is still measured, but plan changes and peak memory
  inside them are not, unless you pass `nested_plans=True`. Then
  `auto_explain` reports the plan of every statement the function runs,
  so an inner plan change splits the fit and inner sorts report memory.
  It needs a superuser, or a server that preloads `auto_explain` and lets
  your role set it (the Supabase images do), and it slows functions that
  run many statements, so it is off by default.
- **O(n log n) versus O(n).** An in-memory sort touches no buffers.
- **What `work_blocks` counts.** Only buffers of the modelled tables, their
  indexes and TOAST storage. System catalog reads are excluded, so neither
  per-connection cache state nor other sessions' activity on a shared server
  moves the measurement.
