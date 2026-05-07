# Changelog

All notable changes to SqlProof will be documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While SqlProof
remains in `0.x`, minor versions may include breaking changes.

## [Unreleased]

### Added

- **PL/pgSQL coverage contrib** in `sqlproof.contrib.plpgsql_coverage`. Wraps
  the `plpgsql_check` extension to produce per-line and per-function coverage
  reports for PL/pgSQL function bodies exercised by your tests.
  - `coverage_session(db, candidates, *, cluster, ...)` — high-level context
    manager. Filters candidates to installed PL/pgSQL functions, logs drift
    for missing/non-PL/pgSQL names, skips the test cleanly if `plpgsql_check`
    isn't installed or no candidates are profilable, yields
    `(report, installed)`. Recommended entry point.
  - `drive_in_order(installed, drivers, *, cluster)` — sorted iteration over
    a cluster's drivers dict with diagnostic logging on failure.
  - `assert_nonzero_coverage(report, installed, *, cluster, threshold=0.0)` —
    the standard "every named function got > threshold statement coverage"
    assertion.
  - `installed_plpgsql_functions(db, candidates=None, *, schema)` — public
    helper that intersects a candidate list with installed PL/pgSQL
    functions in `schema`. SQL-language and missing names are dropped.
  - `collect_coverage(db, functions=None, *, schema)` — low-level primitive,
    unchanged behavior except for the bug fixes below.

### Fixed

- **`collect_coverage` now enables the `plpgsql_check.profiler` GUC** before
  resetting profiler counters. Without this, `plpgsql_profiler_function_tb`
  returns rows where every `exec_stmts` is NULL — making functions look
  uncovered even after they ran successfully (#8).
- **`collect_coverage` now filters non-PL/pgSQL candidates** out of the
  `functions=` list before reading profiler data. Previously, including a
  `LANGUAGE sql` (or other-language) name caused alphabetically-later
  PL/pgSQL functions in the same call to silently drop from the report (#9).

## [0.1.0a1] - 2026-05-04

First public release. Early-stage alpha — APIs are unstable.

### Added

- **Stateful testing.** New `sqlproof.testing.SqlProofStateMachine` base class
  that wires Hypothesis's `RuleBasedStateMachine` into SqlProof: each example
  leases an isolated `SqlProofClient` via `proof.client_for_dataset(...)`, and
  writes are rolled back between examples. Subclasses define `@rule`s and
  `@invariant`s as usual; `on_setup()` runs once per example with `self.db`
  ready. `self.enter(cm)` adopts a context manager for the example's lifetime
  (useful for JWT claims, savepoints, mocked clocks). Run via
  `proof.run_state_machine(MyMachine, settings=...)`.
- **Supabase contrib helpers** in `sqlproof.contrib.supabase`:
  - `as_supabase_user(db, user_id, role=...)` — context manager that sets
    `request.jwt.claims` for the duration of the block, so PostgREST/Supabase
    helpers (`auth.uid()`, `auth.jwt()`) resolve to the given user. Restores
    prior claim on exit; nested invocations stack and unwind correctly.
  - `seed_supabase_test_users(db, count)` — calls Supabase's auth admin API to
    (re)create N deterministic test users with the
    `sqlproof_<n>@test.invalid` email pattern. Idempotent.
  - `seed_test_users_directly(db, count)` — alternative SQL-only path that
    inserts into `auth.users` via the existing connection. Lets tests run
    when the admin API key is unavailable but the test connection has write
    access to the auth schema (e.g. local Supabase).
- **Dataset generation flexibility:**
  - Shrinkable per-table size strategies (pass a Hypothesis integer strategy
    where `SizeSpec` is accepted; size shrinks alongside data).
  - Column-level overrides keyed by `"<table>.<column>"`: fixed values,
    Hypothesis strategies, or callable derivers.
  - External-table FK sampling: register an `ExternalTableSpec` to draw FK
    values from a live external parent table (e.g. `auth.users`).
- **Project specification document** (`SPEC.md`) capturing mission,
  architecture, API surface, and design constraints.

### Known limitations

- Schema introspection covers tables, columns, FKs, CHECK constraints, UNIQUE
  constraints, and enums. Exclusion constraints, partial unique indexes, and
  generated columns are not yet honored.
- Some Postgres types (range types, composite types, custom domains) are not
  yet supported.
- The pytest plugin entry point exists but the CLI flags and reporter wiring
  are still stabilizing.
- No deprecation policy yet — breaking changes ship freely in `0.x`.

[Unreleased]: https://github.com/alialavia/sqlproof/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/alialavia/sqlproof/releases/tag/v0.1.0a1
