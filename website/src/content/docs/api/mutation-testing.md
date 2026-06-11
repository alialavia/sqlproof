---
title: Mutation Testing
description: API reference for MutationSet, Replace, Drop, run_mutation_tests, and MutationResult.
---

The mutation harness applies deliberate bugs ("mutants") to SQL function
bodies, runs your property suite against each one on a fresh clone
database, and reports every mutant the suite failed to kill. A surviving
mutant is a behavior your tests do not constrain.

For the workflow — why, when, and how to wire it into CI — see the
[mutation testing guide](/guides/mutation-testing/). This page is the API
reference.

```python
from sqlproof import MutationSet, Replace, Drop, run_mutation_tests

mutations = MutationSet.for_function("total_usage", [
    Replace("user_id = p_user", "user_id <> p_user"),
    Replace("COALESCE(SUM(amount), 0)", "COALESCE(SUM(amount), 1)"),
    Drop("WHERE user_id = p_user"),
])

result = run_mutation_tests(
    mutations,
    schema_file="schema.sql",
    database_url="postgresql://postgres:postgres@127.0.0.1:5433/proof_template",
    pytest_args=["tests/test_billing.py", "-q"],
)
result.assert_no_survivors()
```

## Authoring mutants

### `Replace(old, new)`

Replace exactly one occurrence of `old` with `new` in the target
function's body. Application fails loudly if `old` is absent or appears
more than once — a mutant that didn't apply must never count as killed.

### `Drop(pattern)`

Delete exactly one occurrence of `pattern` from the body. Same
absent/ambiguous rules as `Replace`.

### `MutationSet.for_function(name, ops)`

Builds one mutant per op against the named function's body. `MutationSet`
instances support `+` so you can combine sets for several functions:

```python
mutations = (
    MutationSet.for_function("total_usage", [...])
    + MutationSet.for_function("is_admin_in_org", [...])
)
```

### Accepted survivors: `expect_survives` / `reason`

A survivor you have triaged and accepted (an equivalent mutant, or a
dead-code branch you knowingly don't test) is declared on the op, so the
acceptance is reviewable in the diff:

```python
Replace(
    "STRICT", "CALLED ON NULL INPUT",
    expect_survives=True,
    reason="NULL inputs rejected by the API layer before reaching SQL",
)
```

`expect_survives=True` requires a `reason=`; a `reason=` without
`expect_survives=True` is rejected. If a declared survivor is later
*killed*, the outcome is `unexpected_kill` — good news (your tests now
cover it); delete the stale declaration.

### Validation happens before any database work

Every mutant is prepared eagerly when `run_mutation_tests` is called, and
authoring errors raise before a single database is cloned:

- The target function must exist in the schema SQL, exactly once
  (overloads are not supported yet).
- The pattern must match exactly once in the body.
- The mutated body must parse (`pglast` for `LANGUAGE sql`,
  `plpgsql_check`'s parser for PL/pgSQL).
- A mutated body whose AST equals the original's is rejected as a no-op
  mutant; two mutants producing the same AST are rejected as duplicates.
- `BEGIN ATOMIC` bodies are not supported — define the function with
  `AS $$ ... $$`.

Mutant ids are sha256 hashes of the target plus the *normalized AST* of
the mutated body, so reformatting the schema doesn't change identities.

### JSON round-trip

Mutants are serializable artifacts: `MutationSet.to_dict()` /
`MutationSet.from_dict()` round-trip through JSON. The Python API is
authoring sugar over this format — an LLM (or any other tool) can emit
mutants as JSON and you can load and run them without writing Python
op objects by hand.

## `run_mutation_tests(...)`

```python
def run_mutation_tests(
    mutations: MutationSet,
    *,
    schema_file: str | Path,
    database_url: str,
    pytest_args: Sequence[str],
    env_var: str = "SQLPROOF_TEST_DATABASE_URL",
    maintenance_db: str = "postgres",
    hypothesis_seed: int | None = None,
    max_workers: int = 1,
    timeout_s: float | None = 600.0,
) -> MutationResult
```

| Parameter | Description |
| --------- | ----------- |
| `schema_file` | SQL file containing the `CREATE FUNCTION` for every mutated target. Used to *locate and rewrite* the function — the clone gets its schema from the template database, not from this file. |
| `database_url` | The **template database**: schema fully applied, zero open connections. Each mutant runs against a fresh `CREATE DATABASE ... TEMPLATE` clone of it. |
| `pytest_args` | Arguments for the per-mutant pytest subprocess, e.g. `["tests/test_billing.py", "-q"]`. |
| `env_var` | Environment variable set to the clone's DSN in the pytest subprocess. If your suite uses sqlproof's fixtures, set this to `"SQLPROOF_DATABASE_URL"` so the plugin picks up the clone. |
| `maintenance_db` | Always-on database used for `CREATE DATABASE` / `DROP DATABASE` commands. Default `"postgres"` is right for most setups. |
| `hypothesis_seed` | Pins `--hypothesis-seed` for every mutant run. Defaults to a fresh random seed, recorded on every outcome — so every run is replayable either way. |
| `max_workers` | Parallel pytest subprocesses. Clone create/drop serialize on an internal lock; default `1`. |
| `timeout_s` | Per-mutant pytest timeout in seconds (default 600). A hung mutant — the canonical case is a mutated loop condition — becomes an `error` outcome. `None` disables. |

For each mutant, the runner clones the template, applies the mutated
`CREATE OR REPLACE FUNCTION` DDL, runs pytest with `env_var` pointing at
the clone, and drops the clone (`WITH (FORCE)`). There is no restore
path by design — the template is never touched.

## `MutationResult`

`run_mutation_tests` returns a `MutationResult` with one `MutantOutcome`
per mutant.

### Outcome statuses

| Status | Meaning | Gate |
| ------ | ------- | ---- |
| `killed` | Tests failed under the mutant — the suite caught it | pass |
| `survived` | Tests passed under the mutant — untested behavior | **fail** |
| `expected_survivor` | Declared survivor survived as declared | pass |
| `unexpected_kill` | Declared survivor was killed — drop the stale declaration | pass |
| `error` | The run proved nothing (pytest exit ≥ 2, timeout, or infra failure) | **fail** |

Only pytest exit codes 0 and 1 are evidence about a mutant; everything
else (interrupted, internal error, usage error, no tests collected) is
an `error`.

### `MutantOutcome` fields

| Field | Description |
| ----- | ----------- |
| `mutant_id` | Formatting-stable sha256 id (also names the clone database) |
| `target` | Mutated function name |
| `description` | Human-readable op summary, e.g. `total_usage: replace 'a' -> 'b'` |
| `status` | One of the statuses above |
| `pytest_exit_code` | Raw exit code (`None` if the run never reached pytest) |
| `hypothesis_seed` | The pinned seed — replay with `pytest --hypothesis-seed=<seed>` |
| `detail` | Output tail for survivors/errors |

### `assert_no_survivors()`

Raises `SqlProofMutationError` listing every `survived` **and** every
`error` outcome (with exit codes and seeds). Errors fail the gate because
an errored run proves nothing about test strength. `expected_survivor`
and `unexpected_kill` pass.

`result.survivors` and `result.errors` expose the same outcomes as
tuples, and `result.to_dict()` serializes the full report.

## Current limitations

- **Function bodies only.** `MutationSet.for_policy(...)` for RLS
  policies is planned but not in v1 — mutate the helper *functions* your
  policies call (`is_admin_in_org`-style predicates) in the meantime.
- No function overloads, no `BEGIN ATOMIC` bodies.
- The runner needs `CREATEDB` rights, and the template database must
  have zero connections while clones are created.
- Interrupted runs can leave `sqlproof_mutant_*` databases behind.
  Rerunning the same set self-heals; orphans from *edited* mutants need
  a manual `DROP DATABASE IF EXISTS sqlproof_mutant_<id>`.
