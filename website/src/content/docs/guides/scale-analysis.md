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
4. **Stop** when at least `min_points` points fit with R² ≥ 0.98, at
   `max_factor`, or when a probe exceeds `probe_timeout_s`.
5. **Split the fit** wherever the plan's shape changes, and report each
   regime.

Options passed through to the sweep: `max_factor` (default 32),
`min_points` (default 5), `probe_timeout_s` (default 30), `seed`
(default 0) and `columns` (per-column overrides, as for bulk generation).

## Choosing arguments

Functions that take a key need one that exists at every scale. Argument
resolvers are re-run against each freshly loaded dataset:

| Resolver | Picks |
| --- | --- |
| `heaviest("t.col")` | The largest key value. Not the most-referenced row, and no worst-case guarantee. |
| `median_key("t.col")` | The middle key in key order: a typical value. |
| `random_key("t.col", seed=0)` | An arbitrary key, deterministic for a seed. |

Plain values are passed through unchanged.

## Reading the result

| Member | Meaning |
| --- | --- |
| `exponent` | Exponent of the final plan regime. Raises `SqlProofScaleError`, with the reason, when the fit is inconclusive. |
| `r_squared` | Fit quality of the final regime. |
| `regimes`, `plan_flips` | Each plan regime's fit, and where the plan changed. |
| `spills_below(rows)` | Whether a sort or hash spilled to disk at or below `rows`. Raises beyond the measured range rather than guess. |
| `rows_before_timeout(ms)` | Projected row band where the function crosses a timeout. Machine-dependent, unlike `exponent`. |
| `points` | Every probe: factor, rows, work and temp blocks, peak memory, plan hash, time, arguments. |

Each run also writes a JSON artifact to `.sqlproof/scale-runs/` (pass
`artifact_dir=None` to skip). It uses the same envelope as
[mutation runs](/guides/mutation-testing/), so you can keep a history per
function.

## What it cannot see

- **Growth in CPU work alone.** Buffers count pages touched, not CPU. A
  join on `a.v < b.v` over a materialized inner side is quadratic in CPU
  but fits about 1.
- **Plans inside a function.** `EXPLAIN` of `SELECT fn()` shows only the
  outer statement. For PL/pgSQL and non-inlined SQL functions, work is
  still measured, but plan changes and peak memory inside them are not.
- **Flat but noisy functions.** A primary-key lookup whose work wobbles
  by a block or two fails the R² check, so `exponent` raises instead of
  reporting about 0.
- **O(n log n) versus O(n).** An in-memory sort touches no buffers.
- **Runaway probes.** `probe_timeout_s` is checked after a probe returns;
  a probe that never returns hangs the sweep.
- **Raw buffer counts across connections.** Data, plans, arguments and
  the fitted exponent reproduce; raw `work_blocks` can shift by a fixed
  offset between connections.
