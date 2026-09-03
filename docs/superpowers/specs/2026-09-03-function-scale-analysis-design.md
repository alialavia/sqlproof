# Function scale analysis: complexity detection and timeout breaking points

**Status:** design draft; awaiting review
**Date:** 2026-09-03
**Sequence:** open-source local harness first; cloud execution is a
runner-backend change, not a rewrite (same pattern as
`2026-06-10-mutation-testing-design.md`).

## Goal

Tell a developer, before their users do, that a SQL or PL/pgSQL function
will not survive growth:

> `billing.compute_invoice` is **O(n²)** in `invoices`.
> At your current 12K rows it runs in 40ms.
> It crosses the 8s `statement_timeout` at roughly **380K rows**.
> The dominant cost is a sequential scan inside a per-row loop.

Nothing in the ecosystem does this. Everything adjacent is *reactive* —
`pg_stat_statements`, Supabase's Query Performance report, pganalyze and
SolarWinds all analyse queries that have **already run** in production,
which by definition means after the timeout has already hurt someone.
Supabase's own advisors ([splinter](https://github.com/supabase/splinter),
`index_advisor`) are static and pattern-level: they will tell you a policy
re-evaluates `auth.uid()` per row, but never that you cross 8 seconds at
380K rows.

The differentiated claim is a **threshold**, not a lint.

## Competitive position

This shapes scope, so it is recorded explicitly:

- **Fix suggestion is commodity.** Supabase ships splinter (~30 lints,
  including `auth_rls_initplan` and `unindexed_foreign_keys`) and
  `index_advisor` free, in the dashboard the customer already uses.
  We **wrap and rank** those; we do not reimplement them. Competing there
  is a fight against a free, better-positioned incumbent.
- **Bulk data generation is commodity-ish.** DataFiller, SeedBase,
  Seedfast and Misata all generate FK-consistent data at volume. None
  handles CHECK constraints, domains, partial uniques, or deferred-FK
  cycles — the parts SqlProof already owns. DataFiller is also GPLv3
  against our MIT (`pyproject.toml:11`) and is a CLI, not a library.
  We own this layer.
- **The sweep is the moat.** Everything defensible is in generating at
  scale, measuring stably, and fitting a curve.

## Confirmed decisions

1. **Local-first**, shipped inside the `sqlproof` package. The run
   artifact is the wire format a future cloud ingester consumes.
2. **The primary metric is buffers, not wall-clock.** Measured: at a
   fixed dataset, wall-clock varied **1.96×** across runs on an *idle*
   machine while buffer counts varied **1.0007×**. Timing is unusable as
   the growth signal; buffers are effectively deterministic.
3. **Baseline correction is mandatory.** Fitting raw buffer counts gives
   the wrong answer (1.69 for a known-quadratic function). Subtracting the
   fixed per-call cost, measured at n=1, gives 1.994.
4. **A second generation path**, because Hypothesis cannot reach the
   scales involved *affordably*. The entropy cap is soft — raising
   `BUFFER_SIZE` lifts it — but the O(n²) cost curve behind it is not,
   and neither is fixable, because both exist to make shrinking and
   replay work (see Evidence). Search and bulk loading want opposite
   things.
5. **One shared schema layer.** The new path reuses
   `dependency_graph.py`, `constraints.py` and the constraint logic in
   `rows.py` verbatim. Only value production and the write path are new.
6. **`EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on the outer call** is
   sufficient to detect complexity — buffer counters accumulate across
   nested statements. `auto_explain` is needed only to *attribute* cost
   to a specific inner statement, and is deferred to a later phase.
7. **Type knowledge lives in one registry**, with the Hypothesis and
   bulk paths as thin interpreters of it. Divergence between the two
   generators is the project's main risk, and this makes divergence in
   type dispatch structurally impossible rather than merely tested for.
   Consequence: refactoring `strategy_for_type` is the first build task,
   not a later cleanup.

## Architecture

Four units, each independently testable:

1. **Bulk generator** (`sqlproof/generators/bulk.py`) — schema in,
   `COPY`-ready row stream out. Pure; no database access.
2. **Loader** (`sqlproof/scale/load.py`) — streams the generator into
   Postgres via `COPY`, runs `ANALYZE`. The only unit that writes.
3. **Probe** (`sqlproof/scale/probe.py`) — runs one function once under
   `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`, returns a work measurement
   plus a plan-shape hash. Pure given a connection.
4. **Sweep + fit** (`sqlproof/scale/sweep.py`) — drives loader and probe
   across scale points, subtracts baseline, fits the exponent, segments
   across plan flips, projects the breaking point. No SQL, no I/O.

Data flow:

```
schema ─▶ bulk generator ─▶ loader (COPY + ANALYZE) ─▶ probe (EXPLAIN)
                                    ▲                        │
                                    └──── sweep controller ◀──┘
                                                 │
                                                 ▼
                                        run artifact (JSON)
                                                 │
                                                 ▼
                                     report / CLI / cloud ingest
```

The sweep controller takes measurements and emits a plain data structure.
That is the seam the cloud service reuses: swap "load locally and probe"
for "load on an ephemeral instance and probe", and the fitting, flip
detection and projection logic is unchanged.

## The bulk generation path

### Why a second path exists

Hypothesis is a *search* engine. Every draw is recorded into structures
that make shrinking and replay possible — the choice sequence, the
decision tree, span boundaries, serialisation to the example database.
That recording is not free, and its cost grows with how much has already
been drawn **within a single example**. Measured per-draw cost doubles
each time the dataset doubles (see Evidence), giving O(n²) overall.
The 8KB `BUFFER_SIZE` entropy cap has the same root: an example must stay
small enough to remain shrinkable.

For finding a 3-row counterexample that breaks an RLS policy, that is an
excellent trade and Hypothesis stays. For filling a table with 500K rows,
it is paying for shrinking machinery on data that will never be shrunk.
There is no flag to disable it; it is the architecture.

### What is reused

Unchanged, imported directly:

- `schema/dependency_graph.py` — FK-safe insertion order, deferred-edge
  cycle resolution
- `generators/constraints.py` — CHECK-constraint refinement
- `rows.py` — `_compile_predicate`, `_composite_unique_keys`,
  `_domain_checks_as_column_checks`

### What is new

- **A single type registry, consumed by both paths.** This is a design
  decision, not a testing one, and it is what prevents divergence.
  Today `strategy_for_type` does two jobs at once: it *holds the type
  knowledge* (int8 spans ±2⁶³, `varchar(n)` caps at n, numeric carries
  precision and scale) *and* emits a Hypothesis strategy. If the bulk
  path re-implements that dispatch, the knowledge exists twice and will
  drift. Instead the knowledge moves into one declarative registry and
  both paths become thin interpreters of it:

  ```python
  TYPE_SPECS: dict[str, TypeSpec]            # single source of truth
  strategy_for_spec(spec) -> SearchStrategy  # Hypothesis interpreter
  sampler_for_spec(spec, rng) -> Callable    # bulk interpreter
  ```

  Adding a type becomes one row in one table. Neither path contains type
  knowledge any more, so updating one and forgetting the other stops
  being possible rather than merely being tested for. Refactoring
  `strategy_for_type`'s 22 branches (4 by type kind — enum, domain,
  range, composite — and 18 by type name) into this shape is the first
  task of the build.
- **Seeded value producers.** The bulk interpreter: a
  `random.Random(seed)`-driven producer per type spec. Reproducible from
  a seed without Hypothesis's replay machinery.
- **`COPY ... FROM STDIN`** via psycopg's `cursor.copy()`, fed by a
  generator so n rows are never held in memory. Replaces the per-row
  `INSERT` in `core.py:345`.
- **Deterministic FK key assignment.** Parent keys are *assigned*
  (1..n_parents) rather than *drawn*, so a child references a valid
  parent by arithmetic without the parent list existing in memory. This
  is what makes the path O(n) and streamable.
- **Skew control.** FK parent selection takes a distribution parameter
  (`uniform` | `zipf(alpha)`). Not cosmetic: real timeouts come from one
  tenant owning a disproportionate share of rows, and uniform data
  systematically under-predicts.
- **`ANALYZE` after load.** Non-optional. Without statistics the planner
  chooses badly and every measurement is meaningless.

### The main risk: divergence between the two paths

Silent divergence is the bug class most likely to eat the schedule — a
bulk dataset violating a constraint the search path respects, surfacing
as a confusing `COPY` failure at 300K rows.

**The contract, stated precisely**, because "consistent" otherwise hides
three different properties:

1. **Validity** — both paths produce data Postgres accepts.
2. **Type coverage** — both paths handle every type the other does.
3. **Statistical shape** — both produce data the *planner* sees
   similarly.

And one thing that explicitly must **not** match: **exact values**. The
bulk path should deliberately produce *less* adversarial data — hunting
unicode edge cases at 500K rows is wasted work. "Same values" is the
wrong contract and must not be tested for.

**Defence in depth, strongest first:**

- **Structural (the type registry above).** Divergence in type dispatch
  becomes impossible rather than merely detectable. Everything below is
  a backstop for what structure cannot cover.
- **Exhaustiveness.** One test walks every `TYPE_SPECS` entry and asserts
  both interpreters produce a value, so a missing branch fails in CI
  rather than on a customer's schema.
- **Postgres is the oracle, not the other path.** Never assert the two
  paths agree with *each other* about validity — they can be identically
  wrong. Assert each independently against a real database: generate,
  load with every constraint enabled, let Postgres reject. "Valid" means
  "Postgres accepted it."
- **Differential testing over generated schemas.** Run both paths over
  the same generated schema and require both to load. This needs
  `sqlproof.testing.schemas()` built out first — see Prerequisites.
- **Statistical parity, specific to this feature.** A plan difference
  invalidates the entire measurement, so what must match is what the
  planner *reads*. Load the same schema both ways at the same size,
  `ANALYZE` both, and diff `pg_stats` — `null_frac`, `n_distinct` and
  `correlation` on any column a query filters or joins on. If the bulk
  path never produces NULLs where the Hypothesis path does, selectivity
  shifts and the sweep may measure a plan users never run. `pg_stats` is
  literally the planner's input, which makes it the right equivalence
  oracle and a mechanically diffable one.

## Measurement

One probe = one `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT fn(...)`.

From the JSON we take:

- **work** = `Shared Hit Blocks + Shared Read Blocks`, summed over the
  plan tree. The growth signal.
- **plan shape hash** — node types and nesting, excluding row counts and
  costs. Changes when the planner switches strategy.
- **execution time** — recorded but *never* fitted on. Used only to
  anchor the final answer in seconds.

Two traps, both real:

- **`actual rows` and `actual time` are per-loop averages.** True work at
  a node is `rows × loops`. This matters because a nested loop reporting
  `rows=50 loops=10000` is exactly where a quadratic hides; read naively
  it looks like 50 rows.
- **Fixed per-call overhead must be subtracted.** Catalog lookups and
  plan caching cost ~100–190 blocks regardless of data size. Measured at
  n=1 per function, once, and subtracted from every point.

## Sweep and fit

1. **Calibrate** — load n=1, probe, record the fixed baseline.
2. **Ladder** — geometric scale points (default ×2 from a base of 1,000)
   up to a configured ceiling or until the timeout budget is exceeded.
   Loading is **incremental**: each point appends to reach the next size,
   so a sweep to 500K costs one 500K generation, not the sum of all
   points.
3. **Fit** — least squares on `log(work − baseline)` against `log(n)`.
   The slope is the complexity exponent. Report R² alongside; a poor fit
   is itself a finding and must not be presented as a confident exponent.
4. **Segment on plan flips.** When the plan-shape hash changes between
   points, fit each regime separately and report the flip. Fitting across
   a discontinuity produces a meaningless exponent.
5. **Project** — extrapolate the final regime's fit to the configured
   timeout budget. Reported as a **range**, never a point estimate, with
   the exponent and R² attached.
6. **Confirm where cheap.** If the projected breaking point is within the
   configured ceiling, load to it and measure directly rather than
   extrapolating. A measured threshold always beats a projected one.

Default timeout budgets follow Supabase's: 3s anon, 8s authenticated,
overridable.

## Run artifact

One JSON file per run, `.sqlproof/scale-runs/<ts>-<id>.json`, following
the mutation-run artifact conventions (`schema_version`, git SHA, schema
fingerprint) so both feed one ingester:

```json
{
  "schema_version": 1,
  "run_id": "…",
  "started_at": "2026-09-03T10:12:04Z",
  "sqlproof_version": "0.10.0",
  "git_sha": "54ad002",
  "schema_fingerprint": "sha256:…",
  "generator_seed": 1234567890,
  "distribution": "zipf(1.2)",
  "functions": [
    {
      "target": "billing.compute_invoice",
      "baseline_blocks": 189,
      "points": [
        {"n": 1000, "blocks": 1204, "exec_ms": 4.1, "plan_hash": "a1b2"},
        {"n": 2000, "blocks": 4310, "exec_ms": 14.0, "plan_hash": "a1b2"}
      ],
      "regimes": [
        {"from_n": 1000, "to_n": 64000, "plan_hash": "a1b2",
         "exponent": 1.99, "r_squared": 0.999}
      ],
      "plan_flips": [],
      "projection": {"timeout_ms": 8000, "breaking_point_rows": [340000, 420000]},
      "advisor_hits": [
        {"source": "splinter", "lint": "unindexed_foreign_keys",
         "relation": "invoices", "rank": 1}
      ]
    }
  ]
}
```

## CLI

```
sqlproof scale <function> [<function> …]
    [--sizes 1000,2000,4000,…]      # explicit ladder, else geometric
    [--max-rows 500000]             # ceiling
    [--timeout-ms 8000]             # budget to project against
    [--distribution uniform|zipf]   # FK skew
    [--seed N]                      # reproducible generation
    [--artifact-dir .sqlproof/scale-runs]
    [--json]                        # machine-readable to stdout
```

Exit non-zero when a projected breaking point falls below a configured
threshold, so it works as a CI gate.

## Error handling and edge cases

- **Function too slow to sweep** — abort the ladder when a single probe
  exceeds a wall-clock cap; report the points gathered and mark the fit
  as truncated rather than hanging.
- **Poor fit (low R²)** — report "could not determine complexity" with
  the raw points. Never present a confident exponent over a bad fit.
- **Plan flips every point** — no stable regime; report the flips as the
  finding and decline to project.
- **Function needs arguments** — require an explicit call expression;
  argument values may reference generated keys.
- **Function with side effects** — run inside a transaction rolled back
  after each probe, reusing the savepoint approach in `core.py:175-179`.
- **Constant-time functions** — exponent ≈ 0 is a valid, reportable
  result, not an error.
- **Schema drift between runs** — annotate via `schema_fingerprint`, as
  the mutation report does.
- **Generation cannot satisfy constraints at scale** — fail loudly naming
  the table and constraint; never silently generate fewer rows than asked.

## Testing strategy

- **Fit layer** carries the bulk of coverage: pure functions over
  synthetic measurement points — exponent recovery, baseline subtraction,
  flip segmentation, low-R² handling, projection ranges. No DB.
- **Known-complexity integration fixtures** — hand-written O(1), O(n),
  O(n log n) and O(n²) functions whose exponents must be recovered within
  tolerance. This is the regression net for the whole feature; a probe of
  it already recovered 1.994 and 1.011 (see Evidence).
- **Cross-path equivalence** — the four backstops enumerated under "the
  main risk": registry exhaustiveness, per-path validation against
  Postgres, differential testing over generated schemas, and `pg_stats`
  parity. Note the last is the only one that is *equivalence* testing;
  the others validate each path independently on purpose.
- **Bulk generator** — per-type round-trip through `COPY` into a real
  Postgres for every `TYPE_SPECS` entry, since the failure mode is a
  value Postgres rejects.
- **Determinism** — same seed produces byte-identical `COPY` output.

## Out of scope

- **Reimplementing splinter or `index_advisor`.** We shell out and rank.
- **`auto_explain` per-statement attribution.** Detection does not need
  it; deferred until "which line is slow" is demanded.
- **Concurrency and lock contention.** This measures single-query
  complexity, not throughput under load. `pgbench` territory.
- **Automatic fix application.** Suggest and rank; never rewrite.
- **The hosted service.** Fan-out, tenancy, billing, PR comments — all
  build on this artifact, none of it here.
- **Wall-clock as a growth signal.** Deliberately excluded; see Evidence.

## Delivery phases

Split into two, each planned and shipped separately.

**Phase 1 — generation foundation.** The type registry refactor, the
bulk generator, the `COPY` loader, the two prerequisites below, and the
full cross-path consistency machinery. This is where the risk
concentrates, so it ships and stabilises before anything is built on it.

It also has standalone value independent of this feature: SqlProof gains
the ability to generate and load large, valid, constraint-respecting
datasets from a schema — useful for seeding development and staging
databases whether or not scale analysis ever ships. Phase 1 is worth
merging even if phase 2 is abandoned.

**Phase 2 — measurement and sweep.** The probe, the sweep controller and
fit, the run artifact, the `sqlproof scale` CLI, and splinter /
`index_advisor` ranking. Every measurement decision in this spec belongs
to phase 2 and is unblocked by phase 1 completing.

## Prerequisites

Two pieces of existing code need work before or alongside the build.
Both stand on their own merits.

### 1. Build out `sqlproof.testing.schemas()`

Today it is effectively a stub: it `del`s its own `max_columns`
argument and every table it emits is identical — a single
`id integer` primary key, with no foreign keys, CHECK constraints,
unique constraints or nullable columns. It varies table *names* and
nothing else.

That matters here because the differential test above is worthless
against it: it would never find a divergence, because it never generates
the constructs where divergence happens. To serve as the cross-path
oracle it needs varied column types drawn from `TYPE_SPECS`,
nullability, CHECK constraints, FK graphs *including the cyclic ones*
`dependency_graph.py` exists to resolve, and composite and partial
unique constraints.

This is a real work item, not a free mitigation, and it is worth doing
regardless — `tests/meta/test_meta_properties.py` currently asserts
meta-properties of the generator against schemas that exercise almost
none of it.

### 2. Hoist loop-invariant strategy construction

Independent of this feature and worth its own issue. `rows.py` rebuilds
loop-invariant objects on every row:

- `rows.py:108` — `draw(st.sampled_from(parents))` constructs a fresh
  strategy over the entire parent list, per row
- `rows.py:123` — `refine_for_checks(column, strategy_for_column(column),
  …)` rebuilds the refinement chain per row per column, uncached

Measured cost: with parent count held at 10, adding one FK column took
generation at n=40,000 from **10.5s to 21.9s**. Hoisting does not change
the asymptotic behaviour — Hypothesis's own bookkeeping dominates — but
it is a constant-factor win benefiting **every existing user** at ordinary
test sizes, not just this feature.

## Evidence

All measured during design (2026-09-03), Postgres 15.8 in the
`sqlproof-pg` container, `hypothesis` 6.152.4.

**Hypothesis per-draw cost grows with example size** — the reason a
second path exists:

| dataset | draws | self time in `_draw` | per draw |
|---|---|---|---|
| 2,500 | 5,000 | 0.04s | 7.5 µs |
| 5,000 | 10,000 | 0.13s | 13.1 µs |
| 10,000 | 20,000 | 0.51s | 25.5 µs |
| 20,000 | 40,000 | 1.94s | 48.6 µs |

Cost per draw doubles with each doubling of the dataset → O(n²) overall.
Located in `hypothesis/internal/conjecture/data.py:788`, with supporting
time in `datatree.py:draw_value`, `start_span`/`stop_span` and
`database.py:choices_to_bytes` — all shrinking/replay bookkeeping.
Default `BUFFER_SIZE = 8 * 1024` (`engine.py:101`) caps a 2-table,
3-column dataset between 1,300 rows (0.24s) and 1,400 (fails after 60s);
raising it lifts the cap but not the exponent. Projected cost at the
default path: ~9 min at 100K, ~3 hours at 500K, before a single row
reaches Postgres.

**Exponent recovery works, with baseline correction:**

| function | raw fit | baseline-corrected | truth |
|---|---|---|---|
| quadratic | 1.69 | **1.994** | 2 |
| linear | 0.34 | **1.011** | 1 |

**Buffers are stable, wall-clock is not** — same function, same data,
n=16,000:

| run | idle wall-clock | blocks |
|---|---|---|
| 1 | 86.2 ms | 8348 |
| 2 | 44.0 ms | 8342 |
| 3 | 57.1 ms | 8342 |

1.96× spread in time on an idle machine; 1.0007× in blocks. Under 8 CPU
spinners, blocks were identical across three runs.

**Nested work is captured by the outer `EXPLAIN`** — buffer counts for a
PL/pgSQL function looping internally grew 318 → 32,830 across the sweep,
confirming no `auto_explain` log parsing is needed for detection.

## Open questions

1. **Multi-table sweeps.** Which table's row count is "n" when a function
   touches several? Proposal: sweep one designated driver table, hold
   others at a fixed ratio, and record the ratio in the artifact.
2. **Argument selection.** For functions taking arguments, which values?
   A poorly-chosen tenant id measures the empty case. Proposal: draw from
   the generated data, and prefer the heaviest key under skew.
3. **Cost ceiling.** A sweep to 500K on a wide schema is minutes of
   compute. Local default ceiling likely wants to be lower than what the
   cloud tier would allow.

## Relationship to the cloud offering

This is the local proof of the signal the cloud tier sells. The artifact
is the wire format a remote ingester consumes; the fit, flip detection
and projection logic is reused verbatim. The cloud tier adds fan-out
across ephemeral instances (a sweep is embarrassingly parallel across
functions), hosted history, PR comments — none implemented here, all
accommodated.
