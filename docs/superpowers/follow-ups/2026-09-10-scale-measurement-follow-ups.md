# Scale measurement: follow-ups

**Status:** open. Drafted as GitHub issues but not yet filed. Each numbered section below is written to be pasted as one issue.
**Date:** 2026-09-10
**Context:** follows Phase 2 of function scale analysis (`sqlproof.scale`, PR #110, stacked on the spec PR #109), from the whole-branch final review and the specs:
- `docs/superpowers/specs/2026-09-09-scale-measurement-design.md` (Phase 2)
- `docs/superpowers/specs/2026-09-03-function-scale-analysis-design.md` (the umbrella design)

## Suggested order

| Stage | Items |
|---|---|
| Before merging #110 | 1 |
| Make the gate trustworthy | 2, 3 |
| Make it usable | 4, 5, 6 |
| Deepen the measurement | 7, 8, 9, 10, 11 |
| Separate track (Phase 1 follow-up) | 12 |
| Housekeeping | 13, 14, 15 |

Items 2 and 3 are the ones that decide whether `scale_analysis` can be advertised as a CI gate. False alarms (item 2) get a gate switched off; missed regressions (item 3) quietly erode trust in it.

---

## 1. Row-level security can hide rows from the sweep's emptiness check

**Label:** `bug`. **When:** before merging #110.

**Problem.** Before its first truncate, `run_sweep` refuses to touch non-empty tables unless `truncate_existing=True`. It decides "empty" with `load.row_counts`, which runs `count(*)` as the connection's role. Row-level security applies to that count but not to `TRUNCATE`. So a table whose rows are all hidden from that role looks empty and passes the check, and then every row is deleted. Two setups cause this:
- a table with `FORCE ROW LEVEL SECURITY` (which applies to its owner);
- a role that holds TRUNCATE but not BYPASSRLS.

**Evidence.** Reasoned from documented Postgres behaviour during the fix round's re-review; not yet reproduced live.

**Fix.** Count inside a transaction under `SET LOCAL row_security = off`. When a policy would filter the query, Postgres then raises ("query would be affected by row-level security policy") instead of returning a filtered count. Treat that error as "cannot verify the table is empty", and refuse unless `truncate_existing=True`.

**Done when:** a live test with a FORCE-RLS table that holds rows makes `run_sweep` refuse, and the rows survive.

## 2. The fit refuses flat but noisy functions

**Label:** `bug`.

**Problem.** `fit_exponent` accepts a fit only when R² ≥ `MIN_R_SQUARED` (0.98). For a flat series with a little noise, R² is near zero by construction. So a well-behaved O(1) function is refused, and `assert result.exponent < 1.5` fails CI for good code.

A second defect makes it worse. `fit_exponent` sets `r2 = 1.0 if ss_tot == 0 else ...`, an exact float comparison. Through rounding, some perfectly flat series above the baseline get R² 0.000 and are refused, e.g. work of 11 blocks at all five points against a baseline of 4. That contradicts the comment in `fit_exponent` saying such a series fits exponent 0.0.

**Evidence.** A plpgsql primary-key lookup swept with `heaviest`: work of 10–13 blocks against a baseline of 4, with R² between 0.00 and 0.35 across runs. The slope was −0.08 ± 0.065, confidently flat. Pinned by the non-strict xfail `test_a_primary_key_lookup_recovers_exponent_near_zero`.

**Fix.** Accept or refuse on the slope's standard error (a confidence interval on the exponent) instead of R², and handle an all-equal work series explicitly before taking logs. This changes the spec's acceptance rule ("Sweep and stop conditions", and "Time-work" under "The two axes"), so update the spec in the same change.

**Done when:**
- the primary-key lookup test passes, and its xfail is removed;
- the linear, quadratic and constant reference functions still recover their exponents;
- the sweep's stop condition uses the new rule.

## 3. Growth in CPU work alone is invisible to the exponent

**Label:** `enhancement`. **Needs a spec decision.**

**Problem.** The exponent is fitted on buffer blocks touched. Work that burns CPU without touching new pages doesn't show up in it: a nested loop over a `Materialize`, a large in-memory sort, hash-heavy work.

**Evidence.** `SELECT count(*) FROM items a JOIN items b ON a.v < b.v`, wrapped in a function, fitted an exponent of 1.04 (R² 0.9998). Over the same 16× range, wall-clock time went from 1.6 ms to 254 ms, about n^1.84. Pinned by the strict xfail `test_a_cpu_only_quadratic_recovers_exponent_near_two`.

**Fix (proposal).** A refuse-only guard. When the wall-clock exponent exceeds the work exponent by a margin, above a minimum execution time, raise `SqlProofScaleError`, explaining that CPU cost is growing faster than I/O. Wall-clock time is never reported as the exponent, so the gate stays machine-independent. The Phase 2 plan reserves execution time for the timeout projection, so this is a spec change.

**Done when:**
- the CPU-only case raises a clear refusal, and its test asserts that instead of being an xfail;
- none of the existing reference functions is refused on a noisy machine.

## 4. Document scale analysis

**Label:** `documentation`.

**Problem.** No website page or README section describes `scale_analysis`. The next release's changelog will list `feat(scale)` with nowhere to read about it.

**Include:**
- the worked example from the spec;
- the data-safety warning: the sweep empties and repopulates the modelled tables, so point it at a test database, and `truncate_existing` opts in to deleting existing rows;
- what the exponent measures (growth in buffer I/O) and what it doesn't (items 3 and 8);
- that `spills_below` refuses beyond the measured range;
- the run artifacts in `.sqlproof/scale-runs/`.

**Done when:** a website docs page exists, linked from the README, and its example runs as written.

## 5. `sqlproof scale report`: a CLI over saved runs

**Label:** `enhancement`.

**Problem.** The umbrella spec's Phase 2 includes a `sqlproof scale` CLI; #110 ships the Python API only. The Phase 2 spec reframed the CLI as `sqlproof scale report` over saved artifacts, mirroring `sqlproof mutation report`.

**Fix.** Read `.sqlproof/scale-runs/*.json` and render per-function history, in the mutation report's style: exponent and R² over time, spill points, plan changes. Keep the assertion API as the CI gate. The umbrella spec's `sqlproof scale <function>` gate command can follow, if wanted.

**Done when:** `sqlproof scale report` renders the runs in a directory, with tests like the mutation report's.

## 6. Rank splinter and `index_advisor` hits alongside scale results

**Label:** `enhancement`.

**Problem.** The umbrella spec's Phase 2 also includes ranking fix suggestions from Supabase's static advisors (`advisor_hits` in its artifact example). Without them, a result says "this is quadratic" but not "add an index on `invoices.customer_id`".

**Fix.** Shell out to splinter and `index_advisor` (the umbrella spec rules out reimplementing them), attach the hits for relations the function touches, rank them, and record them in the artifact.

**Done when:** a function over an unindexed foreign key gets the matching advisor hit ranked first in its result and artifact.

## 7. Make `heaviest` a real worst case

**Label:** `enhancement`.

**Problem.** `run_sweep` always loads the bulk generator's default uniform distribution, and `heaviest` returns the largest key value.
- Under uniform data, the largest key is an arbitrary parent.
- Under zipf, the most-referenced parent is key 1, the smallest.

So the worst case is not what gets measured. The docstrings and the artifact's per-argument labels already say so honestly.

**Fix.**
- Expose `distribution` on `run_sweep` and `scale_analysis` (the loader already accepts it), and record it in the artifact.
- Make `heaviest` count referencing rows through the FK graph (`GROUP BY <fk> ORDER BY count(*) DESC`) instead of taking the maximum key.

**Done when:** under zipf, `heaviest` resolves to the most-referenced parent, and the artifact records the distribution.

## 8. See plans inside functions

**Label:** `enhancement`. **Needs a privilege check.**

**Problem.** EXPLAIN of `SELECT fn()` shows only the outer statement. For a function Postgres doesn't inline — which means any function that reads a table — the plan hash is the same at every point. So plan-change splitting, peak memory and naming the dominant relation never see inside it.

**Evidence.** `find_one()` measured at 500 and at 50,000 rows. The inner query changed from a bitmap heap scan to an index-only scan, while the probe saw a bare `Result` node both times. Pinned by the strict xfail `test_a_flip_inside_a_function_is_segmented`.

**Fix.** Enable `auto_explain` with `log_nested_statements = on` and `log_level = notice`. Collect the nested plans through psycopg's notice handler, and include them in the plan hash and the peak-memory walk.

First check whether the `postgres` role in the Supabase image, which is not a superuser, can load and configure `auto_explain`, or whether it needs grants or preloading.

**Done when:** the plan-flip xfail passes, and peak memory is reported for sorts inside functions.

## 9. Run sweeps in a TEMPLATE clone

**Label:** `enhancement`.

**Problem.** The sweep truncates and reloads the proof's own tables. The guard added in #110 prevents accidents, but it still requires an empty schema or an explicit opt-in to data loss (`truncate_existing=True`).

**Fix.** Sweep in a throwaway database created from a template, as mutation testing already does. That includes the Supabase-image quirks: `supabase_admin` is the superuser, and `pg_cron`/`pg_net` sessions block `CREATE DATABASE ... TEMPLATE`. `truncate_existing` then becomes unnecessary.

**Done when:** `scale_analysis` against a database with seeded tables runs without touching them.

## 10. Cancel runaway probes

**Label:** `enhancement`.

**Problem.** `probe_timeout_s` is checked after a probe returns, so a runaway probe runs to completion before the sweep stops.

**Fix.** Issue `SET LOCAL statement_timeout` after the probe's SAVEPOINT; the existing ROLLBACK TO SAVEPOINT undoes it. Treat a cancelled probe as the end of a truncated sweep, and decide whether to keep a partial point (probably not).

**Done when:** a deliberately slow function is cancelled at the timeout, and its result is marked truncated.

## 11. Loose ends from the final review

**Label:** `documentation`.

- **Wrong explanation.** The comments in `tests/integration/test_scale_complexity_live.py`, and the spec's Known limitations, explain when the primary-key xfail can pass by chance, and that explanation is wrong. Some exactly flat series are refused through float rounding, and some non-flat ones are accepted. Correct it together with item 2.
- **Determinism test can't catch an ignored seed.** The sweep determinism test in the same file compares fields that, for these reference functions, don't depend on the seed. Either add a seed-sensitivity check (e.g. `random_key` resolves differently under two seeds), or narrow the test's docstring and the spec's Determinism bullet to "a repeat run has the same shape".
- **Overclaiming docstring.** `save_run`'s docstring says the recorded parameters let a sweep be run again, but `columns` overrides are not recorded. Narrow the claim, or record a fingerprint of the overrides.

## 12. Real type bounds for six kinds, with the matching statistics checks

**Label:** `enhancement`. **Source:** open question 1 of the umbrella spec, which marks it top priority for the generation foundation.

**Problem.** `TypeSpec` carries bounds for `integer`, `decimal` and `text` only. For `date`, `datetime`, `float`, `interval`, `binary` and `vector`, the Hypothesis and bulk generators each hold their own ranges, and those have measurably diverged: dates run 2000–2054 in one and 0066–9988 in the other. The cross-path `pg_stats` check covers only `null_frac`.

**Fix.** Two changes, landed together:
- give `TypeSpec` real bounds for the six kinds;
- extend the statistics check to `n_distinct` and `correlation`.

They have to land together because the new checks would fail today, while the bounds alone would leave the check believing it covers more than it does. This changes the Hypothesis generator's values, so it needs its own review.

**Done when:** both generators read one set of bounds, and the cross-path `n_distinct` and `correlation` checks pass.

## 13. Intermittent `test_mutation_run_kills_and_survives`

**Label:** `bug`.

**Problem.** `tests/integration/test_mutation_live.py::test_mutation_run_kills_and_survives` failed twice during the Phase 2 work (once in Task 3, once in Task 8), and passed each time when run alone and on a full re-run. It is a sibling of #108 (`test_plan_is_valid_or_explicitly_unresolvable`), which also recurred during Phase 2.

**Fix.** Reproduce it under load or with randomized test order, and capture the failure output.

**Done when:** the cause is known and fixed, or the test is made deterministic.

## 14. Discover the functions to measure

**Label:** `enhancement`. **Priority:** low (later phase).

**Problem.** The caller must name each function. The Phase 2 spec defers catalog discovery to a later phase.

**Fix.** Enumerate functions from the catalog, pick argument resolvers from their signatures, and sweep each. Function and column names are already validated against SQL injection, in anticipation of names coming from the catalog.

**Done when:** a single call measures every function in a schema.

## 15. Check whether #11 is stale

**Label:** none (housekeeping).

#11, "Mutation testing harness for SQL function bodies", is still open, although mutation testing shipped in 0.9–0.10. Close it if it's covered, or narrow it to what is left.
