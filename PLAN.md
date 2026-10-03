# ProofLoop plan (Supabase Select hackathon, demo 17:00 PT)

## 1. Inventory (what SQLProof already does)
- **Mutation run:** Python API only, `sqlproof.mutation.runner.run_mutation_tests(mutations, schema_file, database_url, pytest_args, env_var, verify_baseline=True, artifact_dir=None, timeout_s=600)` → `MutationResult`. No CLI for running mutations. The only CLI is `sqlproof mutation report`, which renders HTML from saved runs (`cli.py:_mutation_report`). The pytest plugin (`pytest_plugin.py`) only provides fixtures plus `--sqlproof-database-url`. Usage pattern to copy: `examples/inbox/tests/mutation/test_mutation_inbox.py`.
- **Output:** `MutantOutcome{mutant_id (AST sha16), target, description, status, pytest_exit_code, hypothesis_seed, detail, duration_s}`. `status` is one of killed | survived | expected_survivor | unexpected_kill | error (`mutation/result.py`). `MutationResult.assert_no_survivors()` raises on survived/error. Outcomes come back in input order (sequential, or `pool.map`).
- **Hand-authored mutations:** `Replace(old,new)` / `Drop(pattern)` → `MutationSet.for_function(name, ops)` gives one mutant per op (`mutation/model.py`). `MutationSet.from_dict` loads them from JSON. `apply.py:prepare_mutants` raises `SqlProofMutationError` before any DB work when a pattern is missing ("pattern not found"), ambiguous ("occurs N times; extend it"), a no-op (identical AST), a duplicate, or a body that doesn't parse. Restrictions (`extract.py`): one `CREATE FUNCTION … AS $$…$$` per name, no overloads, no `BEGIN ATOMIC`. Patterns are matched against the verbatim body.
- **Expected survivors:** supported natively. `expect_survives=True` requires a `reason=`, and a declared survivor that gets killed comes back as `unexpected_kill`. But the reason is not carried into `MutantOutcome` or `RunArtifact`, so `prove` has to join it back by index.
- **RLS:** `contrib/supabase.py:as_rls_user(db, user_id, role="authenticated")`. It sets the transaction-local `request.jwt.claims` to `{"sub","role"}` via `set_config(...,true)`, then runs `SET LOCAL ROLE authenticated`, then `RESET ROLE`. It must run inside a transaction (the `db` fixture already is). `as_supabase_user` sets the claims only and does not enforce RLS. Seed users with `seed_test_users_directly` (direct INSERT into `auth.users`, emails `sqlproof_N@test.invalid`). `service_role` isn't needed anywhere.
- **Supabase:** CI runs a `supabase/postgres:15.8.1.040` service (`.github/workflows/ci.yml`). `.github/actions/setup-supabase-test-db` applies `supabase-test-init.sql` (plpgsql_check, plus JSON-claims `auth.uid()/role()/email()/jwt()`).
- **Clone-per-mutant:** `LocalMutationRunner` runs `CREATE DATABASE sqlproof_mutant_<id> TEMPLATE <dbname of database_url>` through `maintenance_db="postgres"`, applies the mutated DDL, runs pytest in a subprocess, then drops the clone. The baseline is checked on a `sqlproof_baseline` clone. Requirements:
  - The role needs CREATEDB (the CI image has it).
  - The template needs zero open connections. This breaks with `supabase start`, because its services hold connections to `postgres`, so run a bare `supabase/postgres` container locally as well.
- **Gotchas:**
  - The runner's default `env_var` is `SQLPROOF_TEST_DATABASE_URL`, but the plugin reads `SQLPROOF_DATABASE_URL`. Pass `env_var="SQLPROOF_DATABASE_URL"`.
  - A missing DSN makes the plugin skip every test, so pytest exits 0 and the baseline counts as "green" with nothing tested.
  - Red baseline: `run_mutation_tests` raises `SqlProofMutationError` (good, but it's unstructured text).
- **Machine-readable output:**
  - `RunArtifact` JSON (`mutation/artifact.py`, `schema_version` 1, via `artifact_dir=`).
  - `MutationResult.to_dict()`.
  - Counterexample JSON (`reporter/json_io.py`).
  - No JUnit of its own, but pytest's `--junitxml` passes through `pytest_args`.

## 2. Gaps (estimate in minutes)
| # | Gap | min |
|---|---|---|
| G1 | `prove` entry point (`src/sqlproof/contrib/prove.py` + `[project.scripts] prove`): args; mutation file `[{id, op, …}]` → `MutationSet`; merge survivors file `{id: reason}` into `expect_survives`/`reason` | 30 |
| G2 | Connection guard: no connection guard exists today. Allow only loopback or socket hosts (plus explicit `--allow-host` for CI service names); reject `*.supabase.co/.com`, poolers, and any host or dbname matching `prod\|staging\|stg` | 15 |
| G3 | Template builder: `DROP/CREATE proofloop_template`, apply `supabase-test-init.sql` and the migrations | 20 |
| G4 | Structured baseline: `LocalMutationRunner(pytest_args+[--junitxml]).run_baseline()`, parse JUnit into `properties[]`, treat 0 passed as inconclusive, then call `run_mutation_tests(verify_baseline=False)` | 25 |
| G5 | Verdict and JSON mapping (join outcomes to ids and reasons by index), exit codes 0/1/2/3 | 25 |
| G6 | `--format md` comment renderer (`string.Template`) | 15 |
| G7 | Toy example `examples/workspace_agent/`: function, happy-path test, `mutations.json`, the repaired property test | 30 |
| G8 | `.github/workflows/proofloop.yml` + demo PR | 40 |
| G9 | CLAUDE.md rule, then a rehearsed agent run | 15+30 |
| G10 | Unit tests for the guard and the verdict mapping | 20 |

## 3. Deliverables
**a. `prove`:**
```
prove --tests T.py --schema F.sql --function NAME --mutations M.json [--expected-survivors S.json] [--migrations DIR] --db-url URL [--format json|md]
```
- Order of checks: guard → prepare mutants → template → baseline → mutants.
- JSON goes to stdout and logs go to stderr.
- Exit codes: 0 pass, 1 needs_attention, 2 fail, 3 inconclusive.

**b. Action:**
- Runs on `pull_request` (paths: `examples/workspace_agent/**`) with the supabase/postgres service and the existing setup action.
- Runs `prove > verdict.json`, then `prove --render-md verdict.json`, then posts one PR comment updated in place via `gh api`.
- The job's exit code is the check status.
- Permissions: `pull-requests: write`.

**c. CLAUDE.md rule:**
- Before saying a DB change is done, run `prove`.
- `needs_attention`: write one property test per survived mutant, using `as_rls_user` and never `service_role`, then re-run.
- `inconclusive`: fix the setup or report it, and don't loop.
- Stop at `pass` or `fail`.
- Only edit test files. Never touch the mutation or survivor files, the migrations, or the function under test, and never add `expect_survives`.
- Show the diff and wait for human approval before committing.

## 4. JSON contract (`proofloop/1`)
```json
{"schema_version":"proofloop/1","verdict":"pass|needs_attention|fail|inconclusive",
 "scope":{"function":"search_workspace_knowledge","schema_file":"…","tests":"…","git_sha":"abc123","git_dirty":false},
 "baseline":{"status":"green|red|empty|not_run","pytest_exit_code":0,"passed":3,"failed":0,"skipped":0},
 "checks_run":["connection_guard","template","baseline","mutation"],
 "properties":[{"name":"test_search.py::test_member_sees_own_docs","outcome":"passed|failed|skipped|error"}],
 "mutations":[{"id":"drop-membership-check","mutant_id":"9f2c…","description":"…: drop '…'",
   "outcome":"killed|survived|expected_survivor|unexpected_kill|error","declared_reason":null,
   "action":"none|add_test|remove_declaration|investigate","hypothesis_seed":123,"duration_s":4.1}],
 "attention":[{"kind":"survived_mutant|stale_survivor|mutant_error|baseline_red|baseline_empty|refused_connection|authoring_error","ref":"drop-membership-check","message":"…"}],
 "summary":"1 of 2 mutants survived: drop-membership-check","sqlproof_version":"…","duration_s":12.3}
```
Changes from your proposed shape:
- Two extra outcomes that SQLProof produces: `unexpected_kill` and `error`.
- `properties` lists the baseline tests only. SQLProof can't attribute which test killed each mutant.

Verdict rules, checked in this order; the first match wins:
1. Guard refused, authoring error, or template error: `inconclusive`.
2. Baseline red: `fail` (mutants are not scored).
3. Zero tests passed: `inconclusive`.
4. Any mutant with `error`: `inconclusive`.
5. Any `survived`: `needs_attention`.
6. Otherwise `pass`. An `unexpected_kill` adds an attention item but stays `pass`.

## 5. Tasks and cut line
1. G2 guard, then G1 CLI skeleton with the mutation and survivor loaders. Bare supabase/postgres running locally on port 5433.
2. G3 template, G4 baseline, G5 verdict.
3. G7 toy: suite green, mutant survives (`needs_attention`), add the cross-workspace test, mutant killed (`pass`). **Checkpoint 12:30.**
4. Swap in your teammate's real `search_workspace_knowledge` migration, plus a `Drop`/`Replace` of the membership predicate.
5. G6 md renderer, then G8 workflow. Demo PR: weak suite gives a red check and a needs_attention comment; push the test, and the check goes green. **Checkpoint 14:00.**
6. G10 minimal tests (guard + verdict table).
7. G9 CLAUDE.md snippet, then rehearse the agent loop twice. **Feature freeze 15:30.**

**Cut line.** After the hackathon:
- An `auth.users` real-data sniff in the guard.
- Per-mutant killer-test attribution (JUnit per clone).
- A parallel `max_workers`.
- A `RunArtifact` history or dashboard link.
- Carrying `reason` into SQLProof core outcomes.
- Mutating RLS policies directly (v1 mutates functions only).
- Packaging as a reusable composite action or Marketplace action.
