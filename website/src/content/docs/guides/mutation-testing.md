---
title: Mutation Testing
description: Measure whether your property suite would actually catch a bug — apply deliberate bugs to SQL functions and require that the tests fail.
---

A passing suite tells you your SQL behaves correctly *for the properties
you wrote*. It doesn't tell you whether those properties constrain
anything. A property whose Python reference model shares the query's bug
passes everything; so does a tautological assertion. Mutation testing
answers the question directly: **if someone changed `>=` to `>` in this
function, would any test fail?**

The harness applies deliberate bugs ("mutants") to SQL function bodies,
runs your suite against each one on a throwaway clone database, and
reports every mutant that survived. A surviving mutant is a behavior
your tests do not constrain.

This matters most for **agent-written tests**: an LLM reviewing tests
shares the writer's blind spots, but a mutant either gets killed or it
doesn't. It's the objective check on test quality that review cannot
provide.

API details live in the [mutation testing reference](/api/mutation-testing/).

## The execution model

```
template database (schema applied, no connections)
        │  CREATE DATABASE ... TEMPLATE   (per mutant)
        ▼
clone ──► CREATE OR REPLACE FUNCTION (mutated body) ──► pytest ──► DROP
```

Each mutant gets a fresh clone, one pytest run, and a drop. The template
is never modified, so there is no "restore the function if pytest
crashes" problem — the worst case is an orphaned `sqlproof_mutant_*`
database that the next run cleans up.

Two consequences worth internalizing:

1. **The template must have zero open connections** while clones are
   created (`CREATE DATABASE ... TEMPLATE` requires it). Close psql
   sessions and dev servers pointed at it.
2. **Your baseline suite must be green.** A killed mutant means "the
   suite failed", so if the suite fails on the unmutated schema, every
   mutant looks killed and the score is meaningless. `run_mutation_tests`
   guards this for you: by default (`verify_baseline=True`) it runs the
   suite once against an unmutated clone first and raises if it isn't green.

## Setup: a dedicated template database

Don't use your live dev database as the template — Supabase local dev
(`supabase start`) keeps connections open to it (so cloning it fails with
*"source database is being accessed by other users"*), and you want a
schema state you control. Build a dedicated, idle template instead.

If your schema is one file, apply it to a fresh database:

```bash
psql "$SUPABASE_DB_URL" -c 'CREATE DATABASE proof_template'
TEMPLATE_URL="${SUPABASE_DB_URL%/*}/proof_template"
psql "$TEMPLATE_URL" -f supabase/schemas/schema.sql
```

If your schema lives across many migrations (the common Supabase case),
snapshot the running database's schema into an isolated template — this can
even be a *separate* Postgres container, so your app's database is never
touched:

```bash
# dump SCHEMA ONLY but KEEP privileges (see the warning below)
docker exec supabase_db_<project> \
  pg_dump -U postgres -d postgres --schema-only --no-owner > schema.sql

psql "$ADMIN_URL" -c 'CREATE DATABASE proof_template TEMPLATE template0'
psql "${ADMIN_URL%/*}/proof_template" -v ON_ERROR_STOP=0 -f schema.sql
```

**Keep the GRANTs.** Do *not* dump with `pg_dump --no-privileges`. RLS
tests connect as the `authenticated` role, which needs the table-level
`GRANT`s your migrations issue. Strip them and every RLS test fails with
`permission denied for table …` *before any mutation runs* — a red baseline
that makes every mutant look "killed" and reports a meaningless false 100%.
`--no-owner` is fine to keep; `--no-privileges` is the trap. (Supabase-only
extensions like `pgsodium`/`supabase_vault` failing to restore onto a plain
image is harmless if your target functions don't use them.)

Then disconnect. The runner connects to `maintenance_db` (default
`postgres`) for the create/drop commands, so it needs `CREATEDB` rights —
the local Supabase `postgres` user and the CI `supabase/postgres` service
container both have them. By default `run_mutation_tests` runs your suite
once against an unmutated clone first (`verify_baseline=True`) and refuses
to proceed if that baseline is red — so a missing-GRANT template (or any
pre-existing suite failure) fails loudly instead of scoring a false 100%.

## A mutation run, marker-gated

The conventional shape is a pytest test marked `mutation`, excluded from
the default run and invoked nightly or on demand:

```toml
# pyproject.toml
[tool.pytest.ini_options]
addopts = "-ra -m 'not mutation'"
markers = ["mutation: meta-tests that score the suite; run with -m mutation"]
```

```python
# tests/mutation/test_billing_mutation.py
import os
import pytest

from sqlproof import MutationSet, Replace, Drop, run_mutation_tests


@pytest.mark.mutation
def test_billing_suite_kills_all_mutants():
    mutations = MutationSet.for_function("get_user_usage_total", [
        Replace("used_at >= p_period_start", "used_at > p_period_start"),
        Replace("COALESCE(SUM(usage), 0)", "COALESCE(SUM(usage), 1)"),
        Drop("AND feature = p_feature"),
    ])
    result = run_mutation_tests(
        mutations,
        schema_file="supabase/schemas/schema.sql",
        database_url=os.environ["SQLPROOF_TEMPLATE_URL"],
        pytest_args=["tests/test_billing.py", "-q"],
        env_var="SQLPROOF_DATABASE_URL",
    )
    result.assert_no_survivors()
```

```bash
pytest -m mutation -v
```

Two wiring details:

- **`env_var="SQLPROOF_DATABASE_URL"`** — the runner sets this variable
  to the clone's DSN in each pytest subprocess. sqlproof's pytest plugin
  reads `SQLPROOF_DATABASE_URL` (then `SUPABASE_DB_URL`), so this is what
  points the inner suite's fixtures at the clone instead of your dev
  database. The default (`SQLPROOF_TEST_DATABASE_URL`) is for suites
  that read the DSN themselves.
- **`pytest_args` selects the *relevant* suite**, not everything. Mutants
  of a billing function should be killed by the billing tests; running
  the whole suite per mutant just multiplies wall-clock time.

## Choosing mutants

Author mutants the way an attacker of your test suite would — each one
encodes a question:

| Mutant | Question it asks |
| ------ | ---------------- |
| `>=` → `>` | Do you test the boundary? |
| `COALESCE(x, 0)` → `COALESCE(x, 1)` | Do you test the empty group? |
| `AND` → `OR` in a predicate | Do you test that *both* conditions gate? |
| `LEFT JOIN` → `JOIN` | Do you test the zero-children case? |
| `Drop("WHERE user_id = p_user_id")` | Do you test cross-user isolation? |

LLMs are good at proposing these — give the model the function body and
ask for semantically interesting mutants; the output is reviewable like
any generated code (and serializable as JSON via
`MutationSet.from_dict`). Keep the list small enough to triage by hand;
five sharp mutants beat fifty operator permutations.

### Mutating RLS

v1 mutates **functions only**, not policies — so test RLS by targeting the
helper predicates your policies call (the `current_user_org_ids()` /
`is_org_owner(...)`-style functions). Weakening a helper and requiring a
kill answers "would my tests catch an RLS hole?" for every policy built on
it. Direct policy mutation (`for_policy`) is planned.

```python
mutations = MutationSet(mutants=(
    # cross-tenant leak: every org's rows become visible
    Mutant("function", "current_user_org_ids",
           ops=(Drop("WHERE user_id = auth.uid()"),)),
    # privilege escalation: any member passes the owner check
    Mutant("function", "is_org_owner", ops=(Drop("AND role = 'owner'"),)),
))
result = run_mutation_tests(
    mutations,
    schema_file="supabase/schemas/orgs.sql",
    database_url=os.environ["SQLPROOF_TEMPLATE_URL"],
    pytest_args=["tests/orgs/test_cross_org_rls.py", "tests/orgs/test_rls_helpers.py"],
    env_var="SQLPROOF_DATABASE_URL",
)
result.assert_no_survivors()
```

A surviving helper mutant is an RLS behavior your suite doesn't constrain —
exactly the cross-tenant or role-escalation hole you most want to find.
(Your template **must** carry the `GRANT`s for this to work — see Setup.)

## Triaging survivors

Every survivor is one of:

1. **A missing test.** The usual case, and a perfectly-scoped task:
   "mutant `Drop('WHERE user_id = p_user_id')` survived
   `tests/test_billing.py`; write a test that kills it." Write the test,
   rerun, watch the mutant die.
2. **An equivalent mutant** — semantically identical to the original,
   never killable. Declare it: `Replace(..., expect_survives=True,
   reason="...")`. The declaration is in the diff, so acceptance is
   reviewable.
3. **A genuinely untested branch you accept** — same declaration, honest
   reason.
4. **Covered by a suite you didn't run.** `pytest_args` selects which tests
   attack each mutant. A helper mutant can survive the suite you pointed at
   it yet be killed by a different file (e.g. a role-weakening mutant
   surviving the cross-org test but caught by a dedicated role test). Before
   filing it as a missing test, re-run the survivor against the broader
   suite — the mutant id and seed in the report reproduce it exactly.

`assert_no_survivors()` also fails on **errored** runs (pytest exit ≥ 2,
timeout): an errored run proves nothing about test strength, and letting
it pass silently would rot the gate.

If a declared survivor starts getting killed (`unexpected_kill`), your
tests improved — delete the stale `expect_survives`.

## CI: nightly, not every push

Property suites are slow and mutation multiplies them by the mutant
count. Run mutation in a nightly lane (or on demand before a risky
refactor), not on every PR:

```yaml
# .github/workflows/nightly.yml (excerpt)
- name: Build template database
  run: |
    psql "$BASE_URL" -c 'CREATE DATABASE proof_template'
    psql "${BASE_URL%/*}/proof_template" -f supabase/schemas/schema.sql
- name: Mutation run
  env:
    SQLPROOF_TEMPLATE_URL: ${{ env.BASE_URL_TEMPLATE }}
  run: pytest -m mutation -v
```

To cut per-mutant cost, register a capped Hypothesis profile in the
suite (`max_examples=25` or so) and select it via `pytest_args` — a
mutant is a coarse bug; killing it rarely needs the full `runs=` budget.
`max_workers=` parallelizes pytest subprocesses if the server has the
headroom.

## Reproducing a result

Every outcome records the Hypothesis seed the run was pinned to (a fresh
random seed is generated and pinned when you don't pass one). To replay
a survivor locally:

```bash
pytest tests/test_billing.py --hypothesis-seed=<seed from the report>
```

against a database with the mutated function applied — or just pass
`hypothesis_seed=<seed>` to `run_mutation_tests` and rerun the set.

## Persisting runs & the dashboard

A single run prints survivors and asserts the gate, but the interesting
signal is **mutation score over time** — is your suite getting stronger?
Pass `artifact_dir=` to persist each run as a JSON artifact:

```python
run_mutation_tests(
    mutations,
    schema_file="supabase/schemas/schema.sql",
    database_url=os.environ["SQLPROOF_TEMPLATE_URL"],
    pytest_args=["tests/test_billing.py", "-q"],
    artifact_dir=".sqlproof/mutation-runs",   # one JSON file per run
)
```

Each artifact records the per-mutant outcomes, the resolved Hypothesis
seed, the schema fingerprint, and the git sha the run was started on. The
directory is append-only and defaults into `.sqlproof/` (gitignored —
commit it deliberately if you want shared history).

Then render a self-contained HTML dashboard from the accumulated runs:

```bash
sqlproof mutation report --runs-dir .sqlproof/mutation-runs --open
```

The report is one offline HTML file (no server, no CDN): a score-over-time
chart annotated where the schema fingerprint changed, a per-target
breakdown, the latest run's survivors — each with a **NEW** badge for
first-seen survivors and a copy-pasteable repro command — and a run log.
Survivors are tracked across runs by their formatting-stable mutant id, so
a survivor that reappears isn't re-flagged as new. Corrupt or
unknown-version artifacts are skipped with a warning rather than failing
the report, and an empty directory still produces a valid "no runs" page.

`--output` controls the file path (default `mutation-report.html`);
`--open` launches it in your browser.

## Troubleshooting

| Symptom | Cause |
| ------- | ----- |
| `source database "..." is being accessed by other users` | Something is connected to the template. Close psql/dev sessions; in CI make sure the schema-apply step's connection closed. On `supabase/postgres` images, `pg_cron`/`pg_net` workers hold sessions on the `postgres` database — if that's your clone source, terminate-and-clone in one `supabase_admin` session (the `postgres` role can't terminate them). |
| Every mutant killed, suspiciously fast | Baseline suite is red, or the inner pytest can't reach the clone (check `env_var` wiring). `verify_baseline=True` (the default) now catches this and refuses to run. |
| `permission denied for table …` in RLS tests | The template is missing table `GRANT`s — you dumped the schema with `pg_dump --no-privileges`. Re-dump with `--no-owner` only (keep privileges). |
| Mutant `error` with exit 4/5 | The subprocess pytest got bad args or collected no tests — check `pytest_args` paths relative to the working directory. |
| Odd pytest behavior only inside mutation runs | The subprocess inherits `PYTEST_ADDOPTS` from CI. Note that `-m 'not mutation'` in `addopts` is fine — it deselects the meta-test, not your property tests. |
| Leftover `sqlproof_mutant_*` databases | An interrupted run. Rerunning the same set self-heals; otherwise drop them manually. |
