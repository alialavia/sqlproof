# Changelog

All notable changes to SqlProof will be documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). While SqlProof
remains in `0.x`, minor versions may include breaking changes.

## [0.2.0](https://github.com/alialavia/sqlproof/compare/v0.1.0...v0.2.0) (2026-05-30)


### Added

* add connection string + DDL, Neon branching, and CI/CD docs ([64f26ee](https://github.com/alialavia/sqlproof/commit/64f26ee6bd7d7ded259dcd8d2ac97e2722379c9f))
* add FK distribution strategies (zipf, uniform, adversarial) ([eee9b37](https://github.com/alialavia/sqlproof/commit/eee9b3768d582b7db22372466777a4ec3db442e7))
* add Python SqlProof package core ([3784cf2](https://github.com/alialavia/sqlproof/commit/3784cf201ab3a3cdb60f41ef4083970f33378ca3))
* add real Postgres execution layer ([55d3f03](https://github.com/alialavia/sqlproof/commit/55d3f039a60f4f13156ddd7d722601bd360b31a7))
* add SqlProof class and refactor runProperty into runChecks ([bc21a4b](https://github.com/alialavia/sqlproof/commit/bc21a4b5a8b1a8dbed5b65e56193a6781ece5d81))
* add SqlProof website (Astro + Starlight, GitHub Pages) ([5521d17](https://github.com/alialavia/sqlproof/commit/5521d174a8395a6c4171c74f6b5d100b3d4cfa9e))
* add SqlProofConnectOptions, CheckOptions, InvariantOptions types ([40fba61](https://github.com/alialavia/sqlproof/commit/40fba61fce2da920ea614163293f55c09a1b2301))
* **ci:** add code coverage via Codecov ([6396b9c](https://github.com/alialavia/sqlproof/commit/6396b9c5814c180129ddfed45c2355804d238568))
* complete class-based API refactor ([1fbd7a9](https://github.com/alialavia/sqlproof/commit/1fbd7a9015685b0d359a8cae3dba09f570874b9f))
* **contrib:** add plpgsql_coverage module with coverage_session helpers ([90390d3](https://github.com/alialavia/sqlproof/commit/90390d3d79769e355b03282f1e298a29a08eba11))
* **contrib:** add Supabase auth helpers (as_supabase_user, seed_test_users_directly) ([46ed01a](https://github.com/alialavia/sqlproof/commit/46ed01a2565892d00bff6e9298d6cac692e0846a))
* **contrib:** as_rls_user + supabase_test_user_ids ([9a1b692](https://github.com/alialavia/sqlproof/commit/9a1b692f44c5781572bc9c55963f1f1065f9b057))
* **contrib:** as_rls_user + supabase_test_user_ids ([03bd01d](https://github.com/alialavia/sqlproof/commit/03bd01d36e8d6e8af89ca54be36e413f47c53828))
* **contrib:** plpgsql_coverage with coverage_session helpers ([53d768a](https://github.com/alialavia/sqlproof/commit/53d768a398726648402d8b492bc700d89c02816e))
* counterexample reporter ([4397c11](https://github.com/alialavia/sqlproof/commit/4397c11bd0bb100a8eb5e63f699095433bc784ac))
* data generators for property-based table population ([dba5e04](https://github.com/alialavia/sqlproof/commit/dba5e04bd2d5e341a62fddb28de9b81957bee5d5))
* enforce more generated constraints ([be7df83](https://github.com/alialavia/sqlproof/commit/be7df83de5e91a16b6f7c0478c2be5675b43cc1a))
* expand CLI reporting outputs ([7ad8492](https://github.com/alialavia/sqlproof/commit/7ad84926f7e2991f5771cb6a4a09a6620dc40ac0))
* export SqlProof as public API; fix customize() merge and remove dead requirePool ([fdfac3b](https://github.com/alialavia/sqlproof/commit/fdfac3ba6c46a6d51a107716eed7fb3e5b725f64))
* **generators:** expand dataset_strategy with overrides and external FK sampling ([c2cef27](https://github.com/alialavia/sqlproof/commit/c2cef277bfc21cd9379cedb082d108cc2726979e))
* implement capability runners ([69892cd](https://github.com/alialavia/sqlproof/commit/69892cdc5183a90c7c1c08e2ab1f4ef2076cef34))
* parse DDL with pglast ([2bdc06a](https://github.com/alialavia/sqlproof/commit/2bdc06a988c3d1a5b387efbf193fafc5dcba0a32))
* pass FK distribution strategy through makeTableArbitrary ([3830299](https://github.com/alialavia/sqlproof/commit/3830299cc91d9be8e0adcf1ac81ca51e98ad878b))
* **plugin:** ship proof / db / supabase_proof fixtures ([4322e81](https://github.com/alialavia/sqlproof/commit/4322e8148ef0728b26829559d7640298a71236cd))
* **plugin:** ship proof / db / supabase_proof fixtures + Supabase docs rewrite ([1996b08](https://github.com/alialavia/sqlproof/commit/1996b0853cffd9d9cb1e4ece93c620273c315df1))
* public API entry point ([25420de](https://github.com/alialavia/sqlproof/commit/25420de5d02e1c6ad59df39214f7a9ca0f40f692))
* run properties through Hypothesis ([4cbe017](https://github.com/alialavia/sqlproof/commit/4cbe0175d937528ada9fc24bb130d9c87badbd87))
* **runner:** [@sqlproof](https://github.com/sqlproof) columns= + dataset, plus runnable Supabase RLS example ([05978e1](https://github.com/alialavia/sqlproof/commit/05978e16120b4a5411c6811e0561622bb47ad374))
* **runner:** [@sqlproof](https://github.com/sqlproof) now accepts columns= and passes dataset ([be16c5b](https://github.com/alialavia/sqlproof/commit/be16c5bccaaca69d7423690be6f8c30c34be3844))
* schema parsing layer ([feba000](https://github.com/alialavia/sqlproof/commit/feba0005a0617f1695e5557c47c4905109d51a34))
* test runner with Testcontainers support ([fbde947](https://github.com/alialavia/sqlproof/commit/fbde947684bd732dcc69b3bfdbe006053dd19017))
* **testing:** add SqlProofStateMachine for Hypothesis stateful tests ([557d55b](https://github.com/alialavia/sqlproof/commit/557d55b654cab7a7aa08c01015aca0066f5b1973))
* update makeDatasetArbitrary to accept per-table row counts and customizations ([b16004d](https://github.com/alialavia/sqlproof/commit/b16004d6f4717895a44d586cac045f3c077ec305))
* **website:** add dark/green theme CSS ([8abf472](https://github.com/alialavia/sqlproof/commit/8abf4720ae1864d3bb61bd8c17a983464cb27319))
* **website:** add landing page ([a567aa0](https://github.com/alialavia/sqlproof/commit/a567aa0201c312b8820e9bbf50c020242a6cce3d))
* **website:** configure custom domain sqlproof.com ([a4d5b4b](https://github.com/alialavia/sqlproof/commit/a4d5b4b639c229cc86be02c649f46a9d4bf347b5))
* **website:** scaffold Astro + Starlight project ([8805ead](https://github.com/alialavia/sqlproof/commit/8805eadafb7dac66e50025a61f59d8ffc604a072))
* **website:** wire PostHog tracking via PUBLIC_POSTHOG_KEY ([bea501e](https://github.com/alialavia/sqlproof/commit/bea501ea8a947c8266d05db7760773f3a6e91185))
* **website:** wire PostHog tracking via PUBLIC_POSTHOG_KEY ([8bd793b](https://github.com/alialavia/sqlproof/commit/8bd793b05bf59d5435d8f4a8de2886ee7160f4b0))


### Fixed

* **ci:** limit fork concurrency to 1 to prevent OOM ([e439c3a](https://github.com/alialavia/sqlproof/commit/e439c3a5aac99e63404ebe4c9474314d26afa5b4))
* **ci:** pass CODECOV_TOKEN for protected branch upload ([41df1e6](https://github.com/alialavia/sqlproof/commit/41df1e61933a0648dd962649ef133df2a41d4265))
* **ci:** prevent OOM in integration tests ([8c546ae](https://github.com/alialavia/sqlproof/commit/8c546ae8b8833af5d19eac6dec1b15eb6f779fa1))
* **ci:** use singleFork instead of maxForks for vitest 1.x ([cfaab66](https://github.com/alialavia/sqlproof/commit/cfaab6634e00a6dbfe83acc70adb6df242816a4e))
* **contrib:** satisfy ruff lint on Python 3.11 build ([46e0f49](https://github.com/alialavia/sqlproof/commit/46e0f49def66f0d4b88e807f4cfba07b5cdeff13))
* **core:** satisfy pyright on Hypothesis stateful import ([216df2c](https://github.com/alialavia/sqlproof/commit/216df2cefa70dd466ebe384bf001476307f1c974))
* **core:** skip insert when dataset has no rows ([996489f](https://github.com/alialavia/sqlproof/commit/996489f1bd6c48dda2eadd8edd038b690bc1840b))
* **core:** skip insert when dataset has no rows ([96a5a05](https://github.com/alialavia/sqlproof/commit/96a5a05483a4ef292e94b5ec8e7d5f9def4a5235))
* cross-checker compliance for mypy + pyright + ruff ([b4ef375](https://github.com/alialavia/sqlproof/commit/b4ef375c3fc28f3aab1ed8c216e07a31f0954b68))
* **plugin:** lazy-import sqlproof inside fixtures to avoid coverage drop ([9247f24](https://github.com/alialavia/sqlproof/commit/9247f24738d813fc89e87989b2ddea16672a7dbb))
* stabilize schema-file tests on external Postgres ([e28543c](https://github.com/alialavia/sqlproof/commit/e28543cf91de866cd61703dc0d1f9033a3b39bee))
* **tests:** set 120s timeout on parser beforeAll hook ([9e4c516](https://github.com/alialavia/sqlproof/commit/9e4c516542eab8540882f2a19497f19cf84ab557))
* **website:** fall back to default PostHog host on empty string ([087b654](https://github.com/alialavia/sqlproof/commit/087b6541d5892ad860695cf2f0e2c0a9109b0357))
* **website:** fall back to default PostHog host on empty string ([427db1a](https://github.com/alialavia/sqlproof/commit/427db1adefcc2ec51d65423a10ff8e87c427be63))
* **website:** fix broken doc links, switch to light theme, add npm disclaimer ([f26d95d](https://github.com/alialavia/sqlproof/commit/f26d95decf747475376ebeaeaa06e99f5a65af51))


### Changed

* **parser:** replace regex SQL parser with DB-based introspection ([080096f](https://github.com/alialavia/sqlproof/commit/080096f80bb778237b1a8a09e29784bb21658f2e))


### Documentation

* add "Five Property Patterns" examples and an honest pgTAP comparison ([6531935](https://github.com/alialavia/sqlproof/commit/6531935cd9ff4a16bfa2ac1dd7b3ac7c82823751))
* add design spec for SqlProof class API refactor ([2f07a38](https://github.com/alialavia/sqlproof/commit/2f07a3890202a225770141106a31044bf82c4456))
* add project spec ([34b7297](https://github.com/alialavia/sqlproof/commit/34b7297f5bd8c5e39a1c53d5d96cb9136a12818b))
* add project specification ([b73b37d](https://github.com/alialavia/sqlproof/commit/b73b37dd9984e9b5105ce7516543e0f3b072a008))
* add README ([ccac787](https://github.com/alialavia/sqlproof/commit/ccac78743990c824bee74178c560d37b20bbc186))
* add SqlProof website design spec ([ff825cf](https://github.com/alialavia/sqlproof/commit/ff825cf8d11f264cb2a7d3aabe157ed91707b6ed))
* add website implementation plan ([243d706](https://github.com/alialavia/sqlproof/commit/243d7068947f58c57d56a52578204e2e27b362c1))
* align website with Python SqlProof API ([f0f70c6](https://github.com/alialavia/sqlproof/commit/f0f70c6e00b2e5d2427b6700939a53689eb4dc43))
* **example:** add Property 4 side-by-side section to README ([31e9640](https://github.com/alialavia/sqlproof/commit/31e9640998ff4991644cda66ee7f31ef2fd78857))
* **example:** pgTAP port of Property 4 for side-by-side comparison ([548af9e](https://github.com/alialavia/sqlproof/commit/548af9ece69b75e992e59bdef01a0498ced6dcbb))
* **example:** pgTAP port of Property 4 for side-by-side comparison ([01bccde](https://github.com/alialavia/sqlproof/commit/01bccdeec055d397a7aa176e89d0a6114aff3167))
* **examples:** supabase_rls — pgTAP-style RLS test as PBT ([2cec52a](https://github.com/alialavia/sqlproof/commit/2cec52a0ee4bcbcc2fd583416ddd94b0f4179cf0))
* fill in GitHub username in website plan ([45c11ed](https://github.com/alialavia/sqlproof/commit/45c11ed78176dacf0dfae2c5f3c059c2b52e7824))
* function-testing case study where pgTAP loses by the widest margin ([ab01bc0](https://github.com/alialavia/sqlproof/commit/ab01bc0c447f1610d4ef191d9af71f462c34fbc5))
* **guides:** rewrite CI/CD guide as a consumer-facing recipe ([#35](https://github.com/alialavia/sqlproof/issues/35)) ([55e1f13](https://github.com/alialavia/sqlproof/commit/55e1f13c0588bea995513735b3dd4e57f4aa017f))
* pivot to the Supabase-founder-with-AI-agent ICP ([543dc10](https://github.com/alialavia/sqlproof/commit/543dc103f40af0499e30e32239210b59ddd8e62e))
* **readme:** cache-bust the PyPI badge URL ([5d7c9f0](https://github.com/alialavia/sqlproof/commit/5d7c9f0e1539546561b0c428e8a1f406f8b07f3b))
* **readme:** link known gaps to GitHub issues ([aba59f4](https://github.com/alialavia/sqlproof/commit/aba59f44fd904fe637f487378fbfba6f2cbfd4bf))
* restore self-referential FK comment in table-generator ([be1231d](https://github.com/alialavia/sqlproof/commit/be1231d4d67673c7f11f35b36cbd646b5f8b38b5))
* showcase the data generation engine as a first-class capability ([f77c152](https://github.com/alialavia/sqlproof/commit/f77c152035c0c792c7a05ca6aad064e90e6bc5f3))
* **spec:** release engineering design (commit convention, release-please, OSS hygiene) ([6724d8c](https://github.com/alialavia/sqlproof/commit/6724d8c1d7a69241170c5813de672d672a920eb6))
* update getting started for Python API ([bbc3d1e](https://github.com/alialavia/sqlproof/commit/bbc3d1ebedcd663e6248a6b99c50274694c34842))
* update README with website link and class-based API ([6d2ae1a](https://github.com/alialavia/sqlproof/commit/6d2ae1ac8e64719ad2cc309b6bd8867094890d49))
* **website:** add all documentation content ([1e4ffd4](https://github.com/alialavia/sqlproof/commit/1e4ffd4afaaeddafdb80207a4b3b160e35df5f40))

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
