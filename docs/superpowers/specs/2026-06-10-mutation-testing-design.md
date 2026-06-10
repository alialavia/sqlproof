# Mutation testing for SQL function bodies

**Status:** draft (design discussion)
**Date:** 2026-06-10
**Issue:** [#11](https://github.com/alialavia/sqlproof/issues/11)
**Sequence:** open-source local harness first; cloud execution is a
runner-backend change, not a rewrite.

## Summary

A harness that applies deliberate bugs ("mutants") to SQL function
bodies and RLS policies, runs the property suite against each, and
reports every mutant the suite failed to catch. A surviving mutant is
a behavior the tests do not constrain.

Two deliberate departures from classic mutation testing tools:

1. **No mutation engine.** We do not parse SQL or build an AST-level
   operator catalog — that is the months-of-work part of #11 as
   originally filed, and it is now commoditized: an LLM (or the user)
   authors mutants as plain text replacements. SqlProof provides the
   part that must be deterministic: apply, run, report, score.
2. **No restore.** Each mutant runs against a fresh database cloned
   from a template (`CREATE DATABASE ... TEMPLATE proof_base`), so the
   "how do we reliably restore the function when pytest crashes
   mid-run?" problem from #11 disappears. Locally mutants run against
   sequential or parallel clones; in the cloud, against ephemeral
   instances. Same model, different worker count.

End goal: mutation runs are embarrassingly parallel (each mutant ×
suite run is independent), which makes the full matrix — every
function × full catalog, nightly — a natural **cloud offering**. The
local harness is designed so that cloud execution only swaps the
runner backend.

## What this enables (use cases)

Each of these is a question that currently has no measurable answer.

### 1. Coverage signal for `LANGUAGE sql` functions

`plpgsql_check` only profiles `LANGUAGE plpgsql`. Pure SQL functions —
billing aggregates, usage rollups, RLS helper predicates — have no
line-coverage concept at all. Mutation score is the first first-class
adequacy signal for them: *"if someone changed `>=` to `>` in
`get_user_usage_total`, would any test fail?"*

### 2. Authorization regression safety for RLS

Mutate a policy predicate — `AND` → `OR`, drop a `USING` clause,
weaken a membership check — and require that the suite fails. This
answers *"would my tests catch an RLS hole?"* directly, instead of
inferring it from input diversity. For the Supabase audience this is
the headline use case: the functions worth mutating first are exactly
the `is_org_owner` / `current_user_org_ids` shapes that gate every
policy.

### 3. Detecting vacuous properties and self-oracle tests

A property whose Python reference model shares the query's bug passes
everything; so does a tautological assertion. Surviving mutants expose
both mechanically. This matters most for **agent-written tests**: an
LLM reviewing tests shares the writer's blind spots, but a mutant
either gets killed or it doesn't. As the fraction of agent-written
test code grows, this is the objective check on test quality that
review cannot provide.

### 4. A closed feedback loop for agents

"Mutant `Drop("WHERE user_id = p_user_id")` survived
`tests/test_billing.py`; write a test that kills it" is a perfect
agentic task: precise target, objective success criterion. The harness
turns vague "improve test quality" instructions into a loop an agent
can hill-climb. Survivor reports are the work queue; `pytest -m
mutation` is the verifier.

### 5. Refactor safety gate

Before letting an agent (or a human) restructure a SQL function,
mutation score answers *"how strong is the safety net?"* A function at
0 killed / 6 survived mutants has no net; refactoring it is faith, not
engineering.

### 6. Suite-rot detection in CI

As agents churn schema and queries, tests can silently stop
constraining behavior while still passing. A nightly mutation run with
score tracking catches the drift — the same role `mutmut` plays for
SqlProof's own Python internals today (see SPEC.md nightly lane).

## API sketch

```python
from sqlproof import MutationSet, Replace, Drop, run_mutation_tests

mutations = MutationSet.for_function("get_user_usage_total", [
    Replace("WHERE feature = p_feature", "WHERE feature <> p_feature"),
    Replace("COALESCE(SUM(usage), 0)", "COALESCE(SUM(usage), 1)"),
    Replace("used_at >= p_period_start", "used_at > p_period_start"),
    Drop("WHERE user_id = p_user_id"),
])

result = run_mutation_tests(mutations, pytest_args=["tests/test_billing.py"])
result.assert_no_survivors()
```

- `Replace`/`Drop` operate on the function body as text. Application
  fails loudly if the pattern is absent or ambiguous — a mutant that
  didn't apply must never count as killed.
- `MutationSet.for_policy("policy_name", ...)` is the RLS analogue.
- An accepted survivor (dead-code branch we knowingly don't test) is
  declared on the mutant: `Replace(..., expect_survives=True,
  reason="...")`, so acceptance is reviewable in the diff.

## Design constraints

These exist so the cloud backend is a drop-in later:

1. **Mutants are serializable artifacts.** A mutant is
   `(target function/policy, ordered text operations)` and must
   round-trip through JSON. The Python API is authoring sugar over
   this format; remote workers consume the format, not the Python.
2. **Runner abstraction.** `run_mutation_tests` delegates to a
   `MutationRunner`. v1 ships local-sequential and local-parallel
   (template-clone per mutant); the cloud runner implements the same
   interface against ephemeral Postgres instances.
3. **Reproducibility.** Each mutant run records the Hypothesis seed
   and SqlProof version, so a survivor reported by a remote worker is
   reproducible locally with one command.
4. **Fresh database per mutant.** Apply schema + mutation to a clone;
   throw the clone away. No savepoints around pytest, no in-place
   restore, no forked-schema bookkeeping.

## Mutation authoring

Explicit lists are the primary API (see sketch). Two optional layers
on top, in priority order:

- **LLM-proposed mutants.** Given a function body, an LLM proposes
  semantically interesting mutants (it understands that
  `COALESCE(SUM(usage), 0)` → `, 1` attacks the empty-group case).
  Output is the same JSON mutant format, reviewed like any generated
  code. This replaces the `MutationCatalog` engine from #11's original
  sketch.
- **A small static catalog** (operator swaps, JOIN-type swaps,
  `FILTER` drops) as a "what might you be missing" pass — only if the
  LLM layer proves insufficient. Not v1.

Equivalent mutants (semantically identical to the original, never
killable) are the classic triage cost of mutation testing. v1 answer:
explicit lists keep N small enough to triage by hand, and
`expect_survives` records the verdict. LLM-assisted equivalence
triage is a cloud-tier feature, not a v1 blocker.

## Speed

Property suites are slow and mutation multiplies them. Mitigations,
in order of leverage:

- **Marker-gated** (`pytest -m mutation`), nightly or on-demand — not
  on every push.
- **Reduced example counts per mutant.** A mutant is a coarse bug;
  killing it rarely needs the full `runs=` budget. Default to a
  capped profile (e.g. 25 examples) with the seed recorded.
- **Template-clone reuse.** Schema setup happens once into the
  template; per-mutant cost is clone + `CREATE OR REPLACE FUNCTION` +
  suite.
- **Parallelism.** Local: N clones on one server. Cloud: the point of
  the offering.

## Cloud offering (final goal)

The paid tier is the orchestration and the report, not the compute —
anyone can shard mutants across a CI matrix, so parallelism is the
enabler, not the moat. What the cloud adds:

- **Fan-out execution**: full catalog × all functions in wall-clock
  minutes, each mutant on an ephemeral Postgres.
- **Mutation score over time**, per function, with PR comments — the
  comp is "Codecov for SQL test strength."
- **Server-side LLM mutant generation** and equivalence triage,
  metered.
- **Privacy story**: SqlProof generates all data synthetically, so the
  service needs schema DDL and test code — never a byte of production
  data. For an audience testing billing and RLS, this is the easy
  version of a hard conversation.

None of this is v1 scope; v1 is the open-source local harness proving
people want the signal.

## Out of scope

- SQL parsing / AST-level mutation engine.
- Automatic equivalent-mutant detection.
- Mutating table DDL, triggers, or queries embedded in application
  code (function bodies and policies only).
- Replacing `plpgsql_check` line coverage for PL/pgSQL — mutation is a
  complement, not a substitute.

## Open questions

- Mutant identity for score tracking over time: hash of
  `(target, operations)`? Renaming a function orphans its history.
- Should `run_mutation_tests` shrink the killed/survived report into
  the standard SqlProof reporter, or stay a separate artifact?
- Does the per-mutant reduced Hypothesis profile need to be
  overridable per mutant (some mutants only die on rare datasets)?
