# Scale measurement: complexity class and space behaviour for SQL functions

**Status:** design draft; awaiting review
**Date:** 2026-09-09
**Phase:** 2 of `2026-09-03-function-scale-analysis-design.md`. Phase 1
(the bulk generation foundation) shipped in 0.11.0.

## Goal

Make a function's scaling behaviour an assertable property, so an
unscalable query fails CI instead of production:

```python
def test_compute_invoice_survives_growth(proof):
    result = scale_analysis(
        proof,
        "billing.compute_invoice",
        sizes={"customers": 100, "invoices": 1000, "line_items": 5000},
        args=[heaviest("customers.id")],
    )
    assert result.exponent < 1.5
    assert not result.spills_below(500_000)
```

## What changed from the Phase 1 spec, and why

Phase 1 described this feature as answering *"at what row count does this
cross your timeout?"* — a breaking point. That is now the **secondary**
output. The primary output is the **complexity class**.

The two are not the same product. A breaking point depends on the data
profile, the hardware, and the timeout setting; change any one and the
number moves. A complexity class is a property of the function itself:
`O(n²)` is true on a laptop, in CI, and in production, whether the table
holds 10K rows today or 10M.

For a regression gate that difference decides the design. `assert
result.exponent < 1.5` means the same thing in two years. `assert
rows_before_timeout(8000) > 500_000` silently changes meaning the day
someone upgrades the CI runner. So the exponent leads, and the timeout
projection is a clearly-labelled convenience derived from it.

This also settles the metric. From the Phase 1 spike: at a fixed dataset,
wall-clock varied **1.96×** across runs on an *idle* machine while buffer
counts varied **1.0007×**. An exponent fitted on buffers is
machine-independent, so the assertion holds on a laptop and a noisy CI
runner alike. Fitted on wall-clock, the gate would flake.

## Confirmed decisions

1. **A Python API, not a CLI.** SqlProof is a testing library; scale
   analysis belongs in a test. A CLI would force a string config
   language (`--arg customers.id:heaviest`) that has to be parsed,
   validated and documented; as an API, a resolver is a function and a
   literal is a literal. Precedent: mutation testing is
   `run_mutation_tests(...)`, with a CLI only for its *report*.
2. **The output is an assertion**, not a document. An artifact is still
   written for trend history and future ingestion, as mutation runs are,
   but it is a side effect.
3. **Named functions only.** The caller names the function under test.
   Catalog discovery is deferred; nothing here precludes it.
4. **One scale factor over a size profile.** The caller gives a baseline
   `sizes` profile and the sweep multiplies the whole profile: 1×, 2×,
   4×, … "n" is the factor. See "Why not per-table sweeps" below.
5. **Two axes: time-work and space.** Buffers measure work done; peak
   memory and temp-block spills measure space. A spill is a cliff, not a
   curve, and must be reported separately.
6. **Stop on fit quality, not a time budget.** Measuring an exponent
   needs enough points to fit a curve, not enough rows to reach
   production scale.

## Why not per-table sweeps

When a function touches several tables, the obvious question is which
table's growth to vary. Three options were considered:

- a designated driver table, others held fixed
- one scale factor across all tables (chosen)
- an independent sweep per table, for per-table sensitivity

The third is rejected because **it buys information the plan already
gives away**. `EXPLAIN` names the relation whose scan dominates, so
sweeping tables independently costs k× the compute to learn something
readable from a single sweep's plan output. The first is rejected because
holding `line_items` fixed while `invoices` grows does not describe any
real system — those grow together.

So the report reads: *"at 8× your stated profile, `compute_invoice`
crosses 8s, and the dominant cost is a sequential scan on `line_items`"* —
one x-axis, with the culprit named from the plan rather than inferred
from the sweep. When the ratios genuinely need to differ, the caller
passes a different `sizes` profile, which is a one-line change.

This also reuses what exists: `load_dataset(conn, schema, sizes)` already
takes exactly this profile.

## Why not static analysis

Deriving a complexity class from a plan without running it is possible in
outline — seq scan is O(n), nested loop O(n·m), hash join O(n+m), and the
tree composes. No mainstream tool does it, and it is the wrong instrument
here for three reasons, in order of severity:

1. **A plan is not a property of the function.** It is a property of the
   function *at the current statistics*. Postgres re-plans as data grows,
   which is the very flip this design segments on. Static analysis reads
   the plan you happen to have.
2. **PL/pgSQL defeats it.** Loop trip counts are data-dependent, and
   dynamic SQL is opaque. Loop bounds are exactly what cannot be
   recovered statically.
3. **The planner's cost estimate is not a complexity class.** It is one
   scalar at one point, derived from statistics — and estimates are
   wrong precisely when it matters, which is much of why measurement is
   needed at all.

Static analysis remains useful as a *complement* (flagging a nested loop
over a sequential scan as suspicious), which is roughly what splinter's
lints already do. Phase 1's spec already commits to wrapping those rather
than rebuilding them.

## Architecture

Five units. `fit.py` is pure on purpose: it carries the bulk of the test
coverage, and it is the seam a cloud tier reuses verbatim.

1. **`scale/probe.py`** — one measurement. Runs the function under
   `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` and returns a
   `ProbePoint`: work, space, and a plan-shape hash. Pure given a
   connection.
2. **`scale/sweep.py`** — drives load → probe across scale factors,
   applying the stop conditions. The only unit that writes to a
   database.
3. **`scale/fit.py`** — pure. Log-log fit, R², plan-flip segmentation,
   spill detection, projection. No SQL, no I/O.
4. **`scale/result.py`** — `ScaleResult`, the assertion surface.
5. **`scale/artifact.py`** — persists a run, reusing the mutation-run
   artifact conventions (`schema_version`, git SHA, schema fingerprint)
   so both feed one ingester.

Data flow:

```
sizes × factor ─▶ load_dataset ─▶ ANALYZE ─▶ probe ─▶ ProbePoint
                       ▲                                  │
                       └──────── sweep controller ◀───────┘
                                        │
                                        ▼
                                    fit ─▶ ScaleResult ─▶ assertions
                                        │
                                        ▼
                                  run artifact (JSON)
```

## Measurement

One probe runs `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT fn(...)`
and extracts:

- **work** — `Shared Hit Blocks + Shared Read Blocks`, summed over the
  plan tree. The time-complexity signal.
- **peak memory** — `Memory Usage` from Sort and Hash nodes.
- **temp blocks** — `Temp Read Blocks` / `Temp Written Blocks`. Non-zero
  means a sort or hash exceeded `work_mem` and spilled.
- **plan shape hash** — node types and nesting, excluding row counts and
  costs. Changes when the planner switches strategy.
- **execution time** — recorded, never fitted on. Used only to anchor the
  timeout projection, and labelled machine-dependent wherever it
  surfaces.

Three traps, all of which have already bitten during Phase 1 or its
spike:

- **`actual rows` and `actual time` are per-loop averages.** True work at
  a node is `rows × loops`. This matters because a nested loop reporting
  `rows=50 loops=10000` is exactly where a quadratic hides; read
  naively it looks like 50 rows.
- **Fixed per-call overhead must be subtracted before fitting.** Catalog
  lookups and plan caching cost ~100–190 blocks regardless of data size.
  Measured at the smallest scale point, once per function. The spike
  measured a *raw* fit of 1.69 against a corrected 1.994 on a function
  that is exactly quadratic — without this the answer is simply wrong.
- **`ANALYZE` after every load is mandatory.** Without fresh statistics
  the planner has no information about the new rows and the plan is not
  the one a user would get.

## The two axes

### Time-work

Least-squares fit of `log(work − baseline)` against `log(factor)`. The
slope is the complexity exponent; R² is reported alongside it. A poor fit
is a finding in itself and must never be presented as a confident
exponent.

Fits are **segmented on plan-shape changes**. When the plan hash changes
between points, each regime is fitted separately and the flip is
reported. Fitting across a discontinuity produces a meaningless number.

### Space

Reported separately, not folded into the exponent, because a spill is a
**cliff**: a sort that fits in `work_mem` is fast, and the moment it
spills performance drops sharply. A curve fitted straight through one
smears the discontinuity and reports a misleadingly gentle exponent.

Two outputs: peak memory as a function of scale, and the **spill point** —
the smallest scale factor at which temp blocks become non-zero. *"O(n log
n), but spills to disk above ~180K rows"* is a different and equally
actionable finding from the exponent alone, and it is the kind that
surprises people in production because everything is fine right up until
it is not.

### The unit for row-count thresholds

Because the sweep scales a whole profile rather than one table, "rows" is
ambiguous on its own. Every row-count figure this design reports — the
spill point, the timeout projection, the arguments to `spills_below` and
`rows_before_timeout` — means **total rows across the profile**:
`sum(sizes.values()) × factor`.

That unit is chosen because it is a single number, it grows linearly with
the factor so conversion is trivial in both directions, and it matches
what a caller means by "how big is my database". A profile of
`{"customers": 100, "invoices": 1000, "line_items": 5000}` is 6,100 rows
at 1× and 195,200 at 32×.

Callers who think in factors rather than rows can use `factor_at_spill`
and pass `max_factor` directly; the two are interchangeable and the
artifact records both.

## Arguments

`args` is a list, one entry per function parameter. Each entry is a
literal, or a **resolver** — a callable re-run against the freshly loaded
data at every scale point.

```python
args=[heaviest("customers.id"), 42, lambda conn: conn.execute(...).fetchone()[0]]
```

Re-running per point is not a refinement, it is required: the dataset is
regenerated at each scale factor, and keys are assigned deterministically
(`_unique_value` gives `id = i + 1`), so a literal that exists at 8× may
not exist at 1×.

Built-in resolvers: `heaviest(column)`, `random_key(column)`,
`median_key(column)`.

**`heaviest` is the default recommendation, and the reason is the
feature's purpose.** The question being asked is "will this fall over?",
so the case to measure is the one most likely to. Under Zipf skew the
heaviest tenant is the worst case, and reporting "fast" because the probe
happened to pick a customer with three invoices would be exactly the
silent-wrong-answer failure this project keeps guarding against.

The cost is that results describe worst-case behaviour, not typical. That
is correct for this feature but must be **labelled** in the artifact so
nobody reads an exponent as a median. `random_key` and `median_key` exist
for callers asking the other question.

Resolved argument values are recorded in the artifact per scale point.
Without that, a surprising result is unreproducible.

## Sweep and stop conditions

1. **Calibrate** — load the smallest scale point, probe, record the fixed
   baseline.
2. **Ladder** — geometric scale factors (1×, 2×, 4×, …) over the caller's
   `sizes` profile. Loading is **incremental**: each point appends to
   reach the next size, so a sweep to 32× costs one 32× generation, not
   the sum of every point.
3. **Stop** at the first of:
   - ≥ 5 points **and** R² ≥ 0.98 — enough is known; stop paying
   - `max_factor` reached (default 32×)
   - a single probe exceeds `probe_timeout_s` (default 30s) — safety
     valve; marks the fit truncated

All three are keyword arguments.

This is the answer to the Phase 1 spec's cost-ceiling question, and it
falls out of leading with the exponent: measuring a complexity class
needs enough points to fit a curve, not enough rows to reach production
scale. The Phase 1 spike recovered a clean quadratic from 2K–32K rows in
under a second of query time per point.

## `ScaleResult`

```python
result.exponent            # float; RAISES if the fit was inconclusive
result.r_squared           # float
result.regimes             # list, one per plan-shape segment
result.plan_flips          # list of (factor, from_hash, to_hash)

result.spill_point_rows    # int | None -- total rows across the profile
result.factor_at_spill     # int | None -- the same point as a factor
result.spills_below(rows)  # bool

result.rows_before_timeout(ms)   # (lo, hi) total rows; machine-dependent
result.points              # raw ProbePoints
```

Every row-count figure above is **total rows across the profile** — see
"The unit for row-count thresholds".

`exponent` **raises** rather than returning `None` when the fit is
inconclusive. Returning `None` would make `assert result.exponent < 1.5`
fail with a `TypeError` that says nothing about why; raising carries the
reason and the raw points.

## Error handling and edge cases

- **Inconclusive fit (low R²)** — `exponent` raises with the reason and
  the points. Never a confident number over a bad fit.
- **Plan flips at every point** — no stable regime; the flips are the
  finding and no exponent is claimed.
- **Truncated sweep** — the fit covers the range actually measured and is
  flagged truncated. Projection beyond it is refused.
- **Function with side effects** — each probe runs inside a savepoint and
  rolls back, reusing the isolation in `core.py:175-179`.
- **Function needs arguments that resolve to nothing** — e.g. `heaviest`
  on an empty table. Fail loudly rather than measuring the empty case,
  which would report "fast" for an untested function.
- **Extrapolation across an unobserved plan flip** — refused.
  `rows_before_timeout` returns a range, labelled machine-dependent, and
  declines entirely when the final regime is truncated.
- **Cyclic schemas** — the bulk loader raises on FK cycles (Phase 1's
  deliberate guard). Scale analysis inherits that limitation; the error
  should name it rather than surfacing a bare loader failure.

## Testing strategy

- **`fit.py` carries the bulk of coverage** as pure functions over
  synthetic measurement points: exponent recovery, baseline subtraction,
  flip segmentation, low-R² handling, spill detection, projection ranges.
  No database.
- **Known-complexity integration fixtures** — hand-written O(1), O(n),
  O(n log n) and O(n²) functions whose exponents must be recovered within
  tolerance. This is the regression net for the whole feature. Each
  fixture must be *verified to actually have* the complexity it claims
  before it can serve as an oracle — the planner will happily optimise a
  naively-written "quadratic" query into something linear.
- **Spill detection** — a sort forced over `work_mem` must produce a
  spill point, and one that fits must not.
- **Plan-flip segmentation** — a query that genuinely flips plan within
  the swept range must yield two regimes rather than one bad fit.
- **Probe parsing** — nested plans, CTEs and subplans, with an explicit
  case pinning `rows × loops` rather than `rows`.
- **Determinism** — same seed and same profile produce the same points.

## Out of scope

- **Catalog discovery.** Named functions only; discovery is a later
  phase and nothing here precludes it.
- **A CLI.** When one arrives it should be `sqlproof scale report` over
  saved artifacts, mirroring `sqlproof mutation report`.
- **Concurrency and lock contention.** This measures single-query
  complexity, not throughput under load. `pgbench` territory.
- **Reimplementing splinter or `index_advisor`.** Phase 1's spec already
  commits to wrapping and ranking them.
- **Fixing the FK-cycle gap** in the bulk loader. Tracked separately.
- **Wall-clock as a growth signal.** Deliberately excluded; see "What
  changed" above.

## Dependencies

**Nothing blocks this work.** An earlier note claimed the type-registry
widening (Phase 1 spec, open question 1) was a prerequisite, on the
grounds that the two interpreters' date ranges diverge ~100× and would
corrupt `n_distinct` and `correlation`. That reasoning does not hold:
**the sweep only ever measures bulk-generated data**, so the Hypothesis
path's ranges never touch a measurement. The registry widening is
consistency and documentation work, worth doing on its own schedule.

Phase 1's per-column overrides (`columns={...}` on `load_dataset`) are a
genuine enabler here: a caller whose real data is shaped differently from
the bulk defaults can say so, and the measurement reflects it.

## Relationship to the cloud offering

Unchanged from Phase 1: the artifact is the wire format a remote ingester
consumes, and `fit.py` is reused verbatim. The cloud tier adds fan-out
across ephemeral instances (a sweep is embarrassingly parallel across
functions), hosted history and PR comments. None of that is implemented
here; all of it is accommodated.
