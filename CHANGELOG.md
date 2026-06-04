# Changelog

All notable changes to SqlProof will be documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While SqlProof
remains in `0.x`, minor versions may include breaking changes.

## [0.7.0](https://github.com/alialavia/sqlproof/compare/v0.6.0...v0.7.0) (2026-06-04)


### Added

* **examples:** inbox sample — multi-tenant Supabase app with 10 buggy-then-fixed recipes ([#79](https://github.com/alialavia/sqlproof/issues/79)) ([0da4feb](https://github.com/alialavia/sqlproof/commit/0da4febffa0bd68f2aed144255524baa69068eaf))

## [0.6.0](https://github.com/alialavia/sqlproof/compare/v0.5.0...v0.6.0) (2026-06-04)


### Added

* **api:** add SqlProof.row_strategy for ad-hoc fixture rows ([#13](https://github.com/alialavia/sqlproof/issues/13)) ([#75](https://github.com/alialavia/sqlproof/issues/75)) ([74e717d](https://github.com/alialavia/sqlproof/commit/74e717d0ca3b0ef06158f74326a509f490242e96))

## [0.5.0](https://github.com/alialavia/sqlproof/compare/v0.4.0...v0.5.0) (2026-06-02)


### Added

* **generators:** support built-in range types ([#4](https://github.com/alialavia/sqlproof/issues/4)b) ([#66](https://github.com/alialavia/sqlproof/issues/66)) ([2980a7b](https://github.com/alialavia/sqlproof/commit/2980a7bd82e2e6154a292dd9c0336d734cdb3e58))
* **generators:** support composite types with recursive resolution ([#4](https://github.com/alialavia/sqlproof/issues/4)c) ([#67](https://github.com/alialavia/sqlproof/issues/67)) ([4e342b0](https://github.com/alialavia/sqlproof/commit/4e342b06521ac60207daffd6211a83339a1aa5f5))
* **generators:** support custom domain types with CHECK inheritance ([#4](https://github.com/alialavia/sqlproof/issues/4)a) ([#65](https://github.com/alialavia/sqlproof/issues/65)) ([090188a](https://github.com/alialavia/sqlproof/commit/090188a6c36e6610aa3da8497d745a5167c7c547))
* **mcp:** ship sqlproof-mcp server with v1 tools ([#59](https://github.com/alialavia/sqlproof/issues/59)) ([0952c48](https://github.com/alialavia/sqlproof/commit/0952c480b8d2168d7e204c42fe4819c84d93d91a))
* **schema:** flag GENERATED ALWAYS AS columns as generated ([#3](https://github.com/alialavia/sqlproof/issues/3)b) ([#63](https://github.com/alialavia/sqlproof/issues/63)) ([152a8b5](https://github.com/alialavia/sqlproof/commit/152a8b5e9ab63666a9b9cad7b044fa02e412b7b6))
* **schema:** parse and surface EXCLUSION constraints ([#3](https://github.com/alialavia/sqlproof/issues/3)c) ([#64](https://github.com/alialavia/sqlproof/issues/64)) ([fa54eb0](https://github.com/alialavia/sqlproof/commit/fa54eb0fdd8997793fe421e2ee22ed5d735caad1))
* **schema:** support partial unique indexes ([#3](https://github.com/alialavia/sqlproof/issues/3)a) ([#62](https://github.com/alialavia/sqlproof/issues/62)) ([155a87b](https://github.com/alialavia/sqlproof/commit/155a87b0a66ae3be600baa84cda883dc5ad657ed))
* **surface:** introduce SurfaceRegistry for function drift detection ([#12](https://github.com/alialavia/sqlproof/issues/12)) ([#68](https://github.com/alialavia/sqlproof/issues/68)) ([073727c](https://github.com/alialavia/sqlproof/commit/073727c72bd8564f1ba6b40863b08fb4e2f2f6a0))

## [0.4.0](https://github.com/alialavia/sqlproof/compare/v0.3.0...v0.4.0) (2026-06-01)


### Added

* **generator:** enforce composite UNIQUE and composite PRIMARY KEY ([#54](https://github.com/alialavia/sqlproof/issues/54)) ([c87ec67](https://github.com/alialavia/sqlproof/commit/c87ec67f1bbd8fc8e5f6e7ed08b6143a32642ca5))
* **schema:** resolve legitimate FK cycles via deferred edges ([#53](https://github.com/alialavia/sqlproof/issues/53)) ([2beecd5](https://github.com/alialavia/sqlproof/commit/2beecd5e38674aad0c5d285858bc132c7937178d))

## [0.3.0](https://github.com/alialavia/sqlproof/compare/v0.2.5...v0.3.0) (2026-06-01)


### ⚠ BREAKING CHANGES

* **plugin:** lock the pytest plugin CLI flag surface to --sqlproof-database-url ([#55](https://github.com/alialavia/sqlproof/issues/55))

### Added

* **plugin:** lock the pytest plugin CLI flag surface to --sqlproof-database-url ([#55](https://github.com/alialavia/sqlproof/issues/55)) ([027d989](https://github.com/alialavia/sqlproof/commit/027d9896464ca3b480d3d2fa4dee74f6a249ab51))

## [0.2.5](https://github.com/alialavia/sqlproof/compare/v0.2.4...v0.2.5) (2026-06-01)


### Documentation

* establish stability and deprecation policy for 0.x ([#51](https://github.com/alialavia/sqlproof/issues/51)) ([77d07da](https://github.com/alialavia/sqlproof/commit/77d07da7ce46ee5a1d1afab54cf9b1669c171ca1))

## [0.2.4](https://github.com/alialavia/sqlproof/compare/v0.2.2...v0.2.4) - 2026-06-01

### Fixed

- **`storage.buckets` missing columns on bare `supabase/postgres` images.**
  The `setup-supabase-test-db` composite action now adds the columns
  Supabase Storage migrations 0008+ ship: `public`, `avif_autodetection`,
  `file_size_limit`, `allowed_mime_types`, `owner_id`, `type` (+ the
  `storage.BucketType` enum). Test code that does
  `INSERT INTO storage.buckets (id, name, public)` now applies cleanly
  against the image, matching managed Supabase semantics. Pinned action
  consumers should update to `@v0.2.4` to pick this up.
  ([#46](https://github.com/alialavia/sqlproof/pull/46))

### Internal

- Enforce that PRs touching `.github/actions/**` use release-triggering
  commit types (`feat`/`fix`/`perf`). Prevents the silent-bypass class
  of bug that #46 itself exposed.
  ([#48](https://github.com/alialavia/sqlproof/pull/48))

## [0.2.3](https://github.com/alialavia/sqlproof/compare/v0.2.2...v0.2.3) (2026-06-01)


### Documentation

* **agents:** pbt-skills prereq, CI/CD section, bootstrap, refreshed anti-patterns ([#42](https://github.com/alialavia/sqlproof/issues/42)) ([f7e2372](https://github.com/alialavia/sqlproof/commit/f7e2372f5d07d3812c3a948689018476347b90ac))

## [0.2.2](https://github.com/alialavia/sqlproof/compare/v0.2.1...v0.2.2) (2026-05-30)


### Documentation

* **website:** drop alpha framing from landing page ([#40](https://github.com/alialavia/sqlproof/issues/40)) ([9845743](https://github.com/alialavia/sqlproof/commit/9845743917d4bdf1dbda311e63545feabd902371))

## [0.2.1] - 2026-05-30

First PyPI-published stable release. Equivalent in code to `v0.2.0`
(which was tagged in git but never reached PyPI because of a
release-tooling configuration that has since been fixed — see PR #39).
This release ships the docs cleanup from PR #37 on top of everything
listed under [0.2.0] below.

### Documentation

- Drop `--pre` install instructions, reframe as pre-1.0
  ([#37](https://github.com/alialavia/sqlproof/pull/37))

## [0.2.0] - 2026-05-30

First stable-track release. APIs are still pre-1.0 (breaking changes may bump
minor versions per the working deprecation policy in [#6](https://github.com/alialavia/sqlproof/issues/6)),
but the alpha pre-release track is over — `pip install sqlproof` works
without `--pre` from this release onward.

### Added

- **Pytest plugin fixtures.** `sqlproof.pytest_plugin` provides `proof`
  (session) and `db` (per-test) fixtures out of the box, plus
  Supabase-flavored `supabase_proof` / `supabase_db` variants that seed a
  deterministic `auth.users` pool and register the table for FK draws.
  DSN resolution chain: `--sqlproof-database-url` →
  `$SQLPROOF_DATABASE_URL` → `$SUPABASE_DB_URL`. Replaces the ~30-line
  `tests/conftest.py` boilerplate the docs previously asked Supabase
  users to copy.
- **PL/pgSQL coverage contrib** (`sqlproof.contrib.plpgsql_coverage`).
  Wraps the `plpgsql_check` extension to produce per-line and
  per-function coverage reports for PL/pgSQL bodies exercised by your
  tests. Includes `coverage_session()` (high-level context manager),
  `drive_in_order()` (sorted driver iteration),
  `assert_nonzero_coverage()`, and lower-level primitives.
- **`@sqlproof` decorator gains `columns=` and `dataset`.** Tests can
  now override individual columns with fixed values, Hypothesis
  strategies, or callable derivers, and receive the generated dataset
  alongside the `db` client — making model-vs-DB comparisons (the
  property-test bread and butter) much easier to write.
- **Supabase RLS helpers** in `sqlproof.contrib.supabase`.
  `as_rls_user(db, user_id)` is a context manager that sets the JWT
  claim GUC and engages RLS via `SET LOCAL ROLE`.
  `supabase_test_user_ids(db)` looks up seeded test users by email
  pattern.
- **`setup-supabase-test-db` GitHub composite action.** Drop-in setup
  for a Supabase-shaped test Postgres in CI: installs `plpgsql_check`
  and applies GoTrue's `auth.uid/role/email/jwt` migration so the bare
  `supabase/postgres` image matches managed Supabase semantics. See
  [the CI/CD guide](https://sqlproof.com/guides/ci-cd/).
- **Cross-org isolation property** in the `supabase_rls` example — a
  fourth property test demonstrating multi-tenant RLS verification
  side-by-side with the pgTAP equivalent.

### Fixed

- **`SqlProof.client_for_dataset({})` no longer raises
  `CircularDependencyError`** on schemas with FK cycles when the
  dataset is empty. The previous behavior called `insertion_order()`
  unconditionally; the fix short-circuits when there's nothing to
  insert.
- **`collect_coverage` enables the `plpgsql_check.profiler` GUC**
  before resetting profiler counters. Without this,
  `plpgsql_profiler_function_tb` returned NULL `exec_stmts` for every
  line — making functions look uncovered even after running
  successfully. ([#8](https://github.com/alialavia/sqlproof/issues/8))
- **`collect_coverage` filters non-PL/pgSQL candidates** out of the
  `functions=` list before reading profiler data. Previously, including
  a `LANGUAGE sql` name caused alphabetically-later PL/pgSQL functions
  to silently drop from the report.
  ([#9](https://github.com/alialavia/sqlproof/issues/9))
- **`supabase_proof` pytest fixture import.** Was lazy-importing
  `ExternalTableSpec` but missing `SqlProof`, raising `NameError` at
  fixture setup.

### Documentation

- **CI/CD guide rewritten** as a copy-paste-ready GitHub Actions recipe
  leading with the Supabase + auth + RLS case and the new composite
  action.
- **`CONTRIBUTING.md`** added with dev setup, commit/PR convention, the
  release process, and the documented path to 1.0.

### Known limitations (still unresolved)

- Generator only enforces single-column UNIQUE / PRIMARY KEY
  constraints; composite uniques are silently ignored.
  ([#26](https://github.com/alialavia/sqlproof/issues/26))
- Schema introspection doesn't yet honor exclusion constraints,
  partial unique indexes, or generated columns.
  ([#3](https://github.com/alialavia/sqlproof/issues/3))
- Some Postgres types (range, composite, custom domains) are not yet
  supported.
  ([#4](https://github.com/alialavia/sqlproof/issues/4))
- Pytest plugin CLI flags and reporter wiring are still stabilizing.
  ([#5](https://github.com/alialavia/sqlproof/issues/5))
- No formal deprecation policy yet.
  ([#6](https://github.com/alialavia/sqlproof/issues/6))

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

[0.1.0a1]: https://github.com/alialavia/sqlproof/releases/tag/v0.1.0a1
