# Scale measurement: complexity class and space behaviour for SQL functions

**Status:** implemented on the `scale-measurement` branch, and
reconciled with what was built after the final review: where the build
overruled the draft, the text below says what the code does, and "Known
limitations" lists what the measurement cannot see.
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
    assert not result.spills_below(50_000)
```

The profile is 6,100 rows at 1×, and a clean fit stops the ladder at
16× (97,600 rows). `spills_below` answers only within the measured
range, so asking about 500,000 rows here would raise rather than pass
vacuously. The sweep empties and repopulates these tables: run it
against a dedicated test database (see "Data safety").

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
5. **Two axes: time-work and space.** Buffers measure work done --
   I/O-visible work, that is: pages touched, not CPU spent (see "Known
   limitations"); peak memory and temp-block spills measure space. A
   spill is a cliff, not a curve, and must be reported separately.
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
readable from a single sweep's plan output. That holds only where the
plan is visible, though: `EXPLAIN` of `SELECT fn()` shows the OUTER
statement, and for a function Postgres does not inline -- every
plpgsql function, and SQL functions with a FROM clause or an aggregate
-- that is a single `Result` node, with the function's own queries
invisible (Ruling AK). For those functions the plan names no culprit,
which weakens this argument; it is not revisited here. The first is
rejected because holding `line_items` fixed while `invoices` grows does
not describe any real system — those grow together.

The intended report reads: *"at 8× your stated profile,
`compute_invoice` crosses 8s, and the dominant cost is a sequential scan
on `line_items`"* — one x-axis, with the culprit named from the plan
rather than inferred from the sweep. **Not built:** the probe records a
plan-shape hash, not the dominant relation, and for an opaque function
it could not see one anyway. When the ratios genuinely need to differ,
the caller passes a different `sizes` profile, which is a one-line
change.

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

- **work** — the ROOT node's `Shared Hit Blocks + Shared Read Blocks`.
  Postgres reports buffer counts cumulatively (not per loop) and
  inclusive of children, and the root's figure also includes every
  statement run inside a called function -- so the root already holds
  the total, and summing over the plan tree would count the same pages
  several times (Ruling A, verified live). The time-complexity signal,
  for I/O-visible work (see "Known limitations").
- **peak memory** — the largest single node's `Sort Space Used` (an
  in-memory sort: a sort whose `Sort Space Type` is `Disk` has spilled,
  and its space is temp, not memory) or `Peak Memory Usage` (a hash).
  Only nodes in the plan the probe sees: for a non-inlined function
  that is the outer `Result`, so peak memory reads 0.
- **temp blocks** — `Temp Read Blocks` / `Temp Written Blocks`, the root
  total like work. Non-zero means a sort or hash exceeded `work_mem` and
  spilled -- seen even inside a non-inlined function, since temp blocks
  accumulate at the root too.
- **plan shape hash** — node types and nesting, excluding row counts and
  costs, of the OUTER statement's plan. Changes when the planner switches
  strategy there; blind to a plan change inside a non-inlined function
  (Ruling AK).
- **execution time** — recorded, never fitted on. Used only to anchor the
  timeout projection, and labelled machine-dependent wherever it
  surfaces.

Three traps, all of which have already bitten during Phase 1 or its
spike:

- **`actual rows` and `actual time` are per-loop averages; buffer counts
  are not.** True rows at a node are `rows × loops` -- a nested loop
  reporting `rows=50 loops=10000` is exactly where a quadratic hides if
  rows are read naively. But the fit reads buffers, which are cumulative
  totals: multiplying them by loops inflates by a factor that grows with
  n and fabricates a quadratic from a linear function. Verified live: a
  Materialize with `loops=5000` reports 1 shared hit, not 5000 (Ruling
  A). So work is the root's buffer count, never multiplied by loops.
- **Fixed per-call overhead must be subtracted before fitting.** Catalog
  lookups and plan caching cost blocks regardless of data size (~100–190
  in the Phase 1 spike; 31 for this suite's linear reference function).
  It is calibrated on a separate one-row-per-table load before the
  ladder, never on a ladder point -- a ladder point fitted against its
  own work subtracts to 0 and refuses every sweep (Ruling N) -- and the
  calibration round runs twice, keeping the second: the first call of a
  plpgsql function pays a one-time compile (43 blocks, against 31 for
  the replan every ladder point pays after its reload) that no ladder
  point pays (Ruling T). The spike measured a *raw* fit of 1.69 against a
  corrected 1.994 on a function that is exactly quadratic — without this
  the answer is simply wrong.
- **`ANALYZE` after every load is mandatory.** Without fresh statistics
  the planner has no information about the new rows and the plan is not
  the one a user would get.

## The two axes

### Time-work

Least-squares fit of `log(work − baseline)` against `log(factor)`. The
slope is the complexity exponent; R² is reported alongside it, and an
exponent is claimed only at R² ≥ 0.98. A poor fit is a finding in itself
and must never be presented as a confident exponent -- which also means
a flat series with a block or two of noise is refused rather than fitted
~0 (see "Known limitations").

Fits are **segmented on plan-shape changes** the probe can see. When the
plan hash changes between points, each regime is fitted separately and
the flip is reported. Fitting across a discontinuity produces a
meaningless number. The hash covers only the outer statement, so a flip
inside a non-inlined function is fitted straight through (Ruling AK).

### Space

Reported separately, not folded into the exponent, because a spill is a
**cliff**: a sort that fits in `work_mem` is fast, and the moment it
spills performance drops sharply. A curve fitted straight through one
smears the discontinuity and reports a misleadingly gentle exponent.

Two outputs: peak memory as a function of scale, and the **spill point** —
the smallest scale factor at which temp blocks become non-zero.
*"Linear, but spills to disk above ~180K rows"* is a different and
equally actionable finding from the exponent alone, and it is the kind
that surprises people in production because everything is fine right up
until it is not.

`spills_below(rows)` answers only within the measured range: True for a
spill observed at or below `rows`, False when none was seen up to
`rows`, and beyond the largest measured total it raises rather than
report "no spill" for rows nobody measured (Ruling AP).

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

**The intended default is the heaviest key, and the reason is the
feature's purpose.** The question being asked is "will this fall over?",
so the case to measure is the one most likely to. Under Zipf skew the
heaviest tenant is the worst case, and reporting "fast" because the probe
happened to pick a customer with three invoices would be exactly the
silent-wrong-answer failure this project keeps guarding against.

**What was built falls short of that, and says so.** `heaviest(column)`
returns the LARGEST KEY VALUE; it never counts referencing rows. The
sweep loads the bulk generator's default uniform distribution, under
which the largest key is an arbitrary parent -- and under zipf the
most-referenced parent is key 1, the smallest key. So no resolver
measures a worst case, and none is labelled one: the artifact records,
per argument position, how it was chosen (`{"kind": "heaviest",
"column": ...}`, `median_key`, `random_key` with its seed, `callable`
or `literal`) instead of a run-wide "worst-case" label (Ruling AO). A
resolver that counts referencing rows, and a sweep that loads skewed
data for it to matter, are deferred.

Resolved argument values are recorded in the artifact per scale point.
Without that, a surprising result is unreproducible.

## Sweep and stop conditions

0. **Check, before touching anything** — see "Data safety" below.
1. **Calibrate** — load one row per table (`min(count, 1)`, so a table
   sized 0 stays empty), probe, and do it twice, keeping the second
   round's work as the fixed baseline (Rulings N, T; see "Measurement").
2. **Ladder** — geometric scale factors (1×, 2×, 4×, …) over the caller's
   `sizes` profile. Each point **reloads from empty**: TRUNCATE, then a
   fresh load at that factor. Appending instead would give rows added at
   a later factor key distributions computed against a different parent
   count, silently changing what the FK arithmetic assumes (Ruling S).
   Total generation is ≈ 2× the largest point (1 + 2 + … + N ≈ 2N), and
   each point's data depends only on the seed and its factor, which is
   what makes a run reproducible -- its buffer counts to within a few
   blocks (see "Known limitations").
3. **Stop** at the first of:
   - ≥ 5 points **and** R² ≥ 0.98 — enough is known; stop paying
   - `max_factor` reached (default 32×)
   - a single probe exceeds `probe_timeout_s` (default 30s) — safety
     valve; marks the fit truncated. Checked after the probe returns:
     a runaway probe is not cancelled.

All three are keyword arguments.

This is the answer to the Phase 1 spec's cost-ceiling question, and it
falls out of leading with the exponent: measuring a complexity class
needs enough points to fit a curve, not enough rows to reach production
scale. The Phase 1 spike recovered a clean quadratic from 2K–32K rows in
under a second of query time per point.

### Data safety

The sweep EMPTIES AND REPOPULATES every table the proof's schema models,
and on the autocommit connection `scale_analysis` opens, every step
commits. So (Ruling AM), before its first TRUNCATE it validates what it
can -- every `sizes` key names a modelled table, `min_points` is at
least 5, the function name is bare identifiers, and the loader can load
the schema at these sizes (no FK cycle it cannot handle, no required
parent left empty) -- then counts every modelled table's rows and
refuses, naming each non-empty table, unless `truncate_existing=True`.
When the sweep ends, successfully or not, it truncates again, leaving
the tables empty rather than full of synthetic rows. Point it at a
dedicated test database. A design that never touches the user's tables
-- running in a TEMPLATE clone, as mutation testing does -- remains
possible and is not precluded.

## `ScaleResult`

```python
result.exponent            # float; RAISES if the fit was inconclusive
result.r_squared           # float
result.regimes             # list, one per plan-shape segment
result.plan_flips          # list of (factor, from_hash, to_hash)

result.spill_point_rows    # int | None -- total rows across the profile
result.factor_at_spill     # int | None -- the same point as a factor
result.spills_below(rows)  # bool; raises beyond the measured range

result.rows_before_timeout(ms)   # (lo, hi) total rows; machine-dependent
result.points              # raw ProbePoints
result.baseline            # the calibrated fixed cost the fit subtracted
result.argument_policy     # per argument position: how it was chosen
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
- **Extrapolation across a plan flip** — refused when the flip is
  observed: each regime is fitted on its own. A flip inside a
  non-inlined function is NOT observed (the probe sees only the outer
  plan), so it is fitted straight through, with only R² ≥ 0.98 standing
  between it and a confident exponent (Ruling AK). `rows_before_timeout`
  returns a range, labelled machine-dependent, and declines entirely
  when the sweep was truncated or a spill was seen.
- **Cyclic schemas** — the bulk loader refuses FK cycles (Phase 1's
  deliberate guard). Scale analysis inherits that limitation, and names
  it: the sweep runs the loader's own planning step before its first
  TRUNCATE and raises `SqlProofUsageError` carrying the loader's message.
- **Tables that already hold rows** — refused, naming each, unless
  `truncate_existing=True` (see "Data safety").

## Testing strategy

- **`fit.py` carries the bulk of coverage** as pure functions over
  synthetic measurement points: exponent recovery, baseline subtraction,
  flip segmentation, low-R² handling, spill detection, projection ranges.
  No database.
- **Known-complexity reference functions** — hand-written O(1), O(n) and
  O(n²) SQL functions whose exponents the fit must recover within
  tolerance. These are calibration weights: the way you check a scale is
  to put a known mass on it. This is the regression net for the whole
  feature. There is deliberately no O(n log n) function: an in-memory
  sort touches no buffers, so buffer counts cannot tell n log n from n
  (Ruling AU).

  *Called "reference functions", not "fixtures", deliberately — `fixture`
  already means `@pytest.fixture` in this test suite, and both appear in
  the same files.*

  **Each reference function must be verified to actually have the
  complexity its name claims before it can serve as an oracle.** The
  planner will happily execute a naively-written "quadratic" query as a
  hash join, making it linear. Measured on this project's test database:
  `SELECT count(*) FROM a, b WHERE a.x = b.x` fits an exponent of **0.95**,
  not 2 — Postgres builds a hash on one side and probes it once per row of
  the other. Buffer-visible quadratic behaviour needs **repeated scans**:
  a procedural `FOR` loop the planner cannot rewrite into a join, over a
  column with no index, re-reads the table once per outer row; that
  shape fits **1.95**. A quadratic that re-reads nothing is invisible: a
  non-equi self-join (`a.v < b.v`) runs as a Nested Loop over a
  Materialize that replays the inner side from memory -- quadratic in
  CPU, yet ~1 in buffers. The suite pins that blind spot, and the
  flat-but-noisy refusal (a primary-key lookup), as **adversarial
  reference functions** in strict xfails (see "Known limitations").

  A wrong oracle is worse than no oracle. It reads as a broken fit, and
  the tempting repair — widening the tolerance until it passes — leaves
  the measurement validated against something that proves nothing.
- **Spill detection** — a sort forced over `work_mem` must produce a
  spill point, and one that fits must not.
- **Plan-flip segmentation** — a query that genuinely flips plan within
  the swept range must yield two regimes rather than one bad fit.
- **Probe parsing** — nested plans, CTEs and subplans, with an explicit
  case pinning that buffer work is the root's count and is NOT
  multiplied by loops (Ruling A); the CTE and InitPlan/SubPlan cases use
  EXPLAIN JSON captured from a live database.
- **Determinism** — pinned live (Ruling AV): two sweeps with the same
  seed and profile, each on its own connection, agree EXACTLY at every
  point on factor, total rows, temp blocks, peak memory, plan hash and
  resolved arguments, for the quadratic reference and for a
  primary-key lookup. On the quadratic reference, work also agrees to
  within 4 blocks per point and the fitted exponents to within 0.01;
  the lookup's work is not compared, since a 4-block tolerance means
  nothing at its ~10 blocks. Exact equality of work is not pinned:
  buffer counts carry a few blocks of run-to-run noise (see "Known
  limitations").

## Known limitations

What the measurement cannot see, or refuses, as built. Every gap a test
can pin is pinned by a strict xfail, so it cannot close -- or stay
open -- silently.

- **CPU-only growth is invisible.** Buffers count I/O-visible work --
  pages touched -- not CPU. A quadratic that re-reads nothing passes the
  gate: the final review swept `SELECT count(*) FROM items a JOIN items
  b ON a.v < b.v` (a Nested Loop over a Materialize) and measured an
  exponent of 1.04 at R² 0.9998 while execution time grew as n^1.84.
  Pinned by `test_a_cpu_only_quadratic_recovers_exponent_near_two`.
  Guarding on wall-clock -- refusing when time grows far faster than
  work -- would change the rule that wall-clock anchors only the
  timeout projection; that is the user's decision (Ruling AS).
- **The probe sees only the outer statement.** `EXPLAIN` of `SELECT
  fn()` shows the plan of that statement, not of the queries a
  non-inlined function runs -- every plpgsql function, and SQL
  functions Postgres does not inline. Work and temp blocks still
  accumulate at the root and are measured, but the plan hash,
  segmentation on plan flips, peak memory and naming the dominant
  relation apply only to plans visible at that level: for an opaque
  function the hash never changes, peak memory reads 0, and an inner
  plan flip is fitted straight through (Ruling AK). Pinned by
  `test_a_flip_inside_a_function_is_segmented`.
- **Flat-but-noisy work is refused.** Against a flat series R² measures
  only the noise, so an O(1) function whose work moves by a block or two
  from point to point -- a primary-key lookup, at R² 0.000 to 0.75 --
  fails the R² ≥ 0.98 acceptance, and `exponent` raises rather than
  report ~0: a false alarm, never a wrong number. Only an exactly-flat
  series is accepted as 0. Pinned by
  `test_a_primary_key_lookup_recovers_exponent_near_zero`. Gating on the
  slope's standard error instead would change the acceptance rule;
  that is the user's decision (Ruling AT).
- **No argument is a worst case.** `heaviest` returns the largest key
  value and the sweep loads uniform data; the artifact labels each
  argument by how it was chosen (Ruling AO; see "Arguments").
- **`spills_below` answers only within the measured range**, and raises
  beyond it (Ruling AP).
- **The sweep is destructive** to the modelled tables, and guarded
  accordingly (Ruling AM; see "Data safety").
- **A runaway probe is not cancelled.** `probe_timeout_s` is checked
  after a probe returns.
- **Buffer counts repeat only to within a few blocks.** Two sweeps with
  the same seed and profile -- on one connection or on fresh ones --
  measured identical row counts, plan hashes, temp blocks, peak memory
  and arguments, but work that differed by up to 2 blocks at a point on
  the quadratic reference (2,862 against 2,864 at 4×, 0.07%; 45,187
  against 45,189 at 16×), and by up to 3 on a primary-key lookup whose
  10–13 blocks reshuffled between runs. The same happens on the code
  before the final review's fix wave, so it comes from Postgres's
  buffer accounting -- most likely catalog lookups, which vary from run
  to run -- not from the sweep's data. So the determinism test pins
  work to within 4 blocks, not exactly (Ruling AV; see "Testing
  strategy").
- **O(n log n) cannot be told from O(n)** by buffer counts: an in-memory
  sort touches no buffers.

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
consumes, and `fit.py` is reused verbatim. The artifact records every
point and the calibrated baseline, so a re-fit needs no database (Ruling
AQ). The cloud tier adds fan-out across ephemeral instances (a sweep is
embarrassingly parallel across functions), hosted history and PR
comments. None of that is implemented here; all of it is accommodated.
