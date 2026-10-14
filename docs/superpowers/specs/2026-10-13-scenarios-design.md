# Scenarios: declarative, reproducible database states

**Status:** phases 1-4 implemented (experimental).
**Date:** 2026-10-13
**Builds on:** bulk generation (`2026-09-03-function-scale-analysis-design.md`,
Phase 1, shipped in 0.11.0) and per-column overrides (#107).

## Goal

Let a developer describe a database state the way Storybook lets them
describe a UI state: pin the rows that matter for a case, and let the
generator fill everything else from a seed. The same scenario, seed,
schema and sqlproof version always produce the same rows.

```python
# scenarios/billing_scenarios.py
from sqlproof.scenarios import scenario

@scenario(seed=42)
def tiered_customers(s):
    s.table("customers").cohort(2, tier=1).cohort(3, tier=2).fill(20)
    s.table("invoices").per_parent("customers", where={"tier": 1}, count=(5, 10))

@scenario(seed=20261003)
def empty_workspace(s):
    s.row("workspaces", name="acme", title="Acme")
    s.row("workspaces", name="initech", title="Initech")
    s.row("auth.users", name="alice")
    members = s.table("workspace_members")
    members.cohort(1, user_id=s.ref("alice"), workspace_id=s.ref("acme"))
    members.cohort(1, user_id=s.ref("alice"), workspace_id=s.ref("initech"))
    # Ten documents under acme, none under initech.
    s.table("workspace_documents").per_parent(
        "workspaces", where=s.ref("acme"), count=10
    )
```

```bash
sqlproof scenarios list
sqlproof scenarios up empty_workspace --dsn "$SQLPROOF_DATABASE_URL"
```

## Why

Three things want the same object.

1. **Developers** want named, repeatable states to develop and debug
   against: "two Tier 1 customers with overdue invoices", "a member of
   an empty workspace". Today they hand-write seed SQL, which drifts
   from the schema, or call the documented data-generation recipe, which
   reaches into the private `sqlproof.core._insert_dataset`.
2. **Tests** want the same states as fixtures, and property tests want
   them as a starting point that Hypothesis varies around.
3. **Hosted tooling** can build on scenarios only if a scenario is a
   stored, serializable, deterministic object. A Python lambda passed to
   `columns=` is none of those.

## What exists today

Most of the machinery is already here; what is missing is a declarative
layer and two small generator hooks.

| Need | Today | Gap |
| --- | --- | --- |
| Row counts per table | `sizes={"customers": 25}` | Keyed by bare table name; `auth.users` and `public.users` collide |
| Pin a column for some rows | Callable override receiving `row_index` on both paths (`ColumnContext`, `BulkColumnContext`) | Works, but a callable cannot hand a row back to the generator, so "the other 20 are random" has to be hand-rolled |
| Deterministic rows | Bulk path: seeded `random.Random`, keys and FKs are pure functions of row index (`_unique_value(column, type, index)`) | Columns with a default (`now()`, `gen_random_uuid()`) are left to Postgres, so they differ on every load |
| Children per parent | Bulk path picks each child's parent with `parent_index_for` (uniform or zipf) | No way to say "each Tier 1 customer has 5–10 invoices" |
| Named rows referenced elsewhere | — | No way to say "alice" once and use her as an FK value and as the caller |
| Load into a real database | `sqlproof.scale.load.load_dataset` (COPY, FK-ordered) | Not exported or documented; no CLI |
| Stored format | — | Nothing serializable |

## Non-goals

- **Copying or sampling production data.** Scenarios are synthetic by
  construction, so a scenario never carries real customer data.
- **Arbitrary cross-row constraints** ("the sum of line items equals the
  invoice total"). Derived columns stay expressible as callable
  overrides in Python scenarios; the YAML form does not carry code.
- **Replacing `sizes=` / `columns=`.** Scenarios compile to them; the
  existing APIs keep working unchanged.
- **Running calls against a scenario and comparing the results across
  commits.** Behaviour snapshots, baselines, comparison and review are
  out of scope for the library. Scenarios give a deterministic state;
  what is run against it is up to the caller (property tests, scale
  sweeps, or external tooling).

## Concepts

**Scenario.** A named, seeded description of a database state: a set of
table specs. Identity is `(name, content hash)`; the
content hash covers the normalized spec, not the Python source.

**Table spec.** For one table, an ordered list of row groups:

- **Cohort** `cohort(n, **pinned)`: `n` rows whose listed columns take
  the given values; every other column is generated.
- **Named row** `row(table, name=..., **pinned)`: a cohort of one that
  can be referenced by name.
- **Fill** `fill(n)`: `n` rows with nothing pinned.

A table's size is the sum of its groups. Rows are laid out in group
order, so a cohort occupies a fixed, known index range. That is what
makes every other feature compile to row-index overrides.

**Reference** `s.ref("alice")`: the primary-key value of a named row.
Usable as a pinned value (an FK column) and as a function argument. Since
the bulk path assigns keys as `_unique_value(pk, type, index)`, a
reference resolves at compile time to a concrete value.

**Per-parent counts** `per_parent(parent, where=..., count=(lo, hi))`:
each parent row matching `where` (a conjunction of pinned values, or a
named row) gets between `lo` and `hi` children, drawn from the scenario
seed. Parents not matched fall back to `default=` (a count or range) or
to zero.

## Python API

```python
from sqlproof.scenarios import scenario, Scenario

@scenario(*, seed: int, name: str | None = None, clock: datetime | None = None)
def build(s: ScenarioBuilder) -> None: ...

class ScenarioBuilder:
    def table(self, qualified_name: str) -> TableBuilder: ...
    def row(self, table: str, *, name: str, **pinned: object) -> RowRef: ...
    def ref(self, name: str) -> RowRef: ...

class TableBuilder:
    def cohort(self, n: int, **pinned: object) -> TableBuilder: ...
    def fill(self, n: int) -> TableBuilder: ...
    def per_parent(self, parent: str, *, where: Mapping[str, object] | RowRef | None = None,
                   count: int | tuple[int, int], default: int | tuple[int, int] = 0,
                   column: str | None = None) -> TableBuilder: ...

class Scenario:
    name: str
    seed: int
    def compile(self, schema: SchemaInfo) -> CompiledScenario: ...
    def load(self, dsn: str, *, schema: str = "public",
             truncate_existing: bool = False) -> LoadReport: ...
    def strategy(self, proof: SqlProof) -> SearchStrategy[Dataset]: ...
    def to_dict(self) -> dict[str, object]: ...   # the serializable form
```

Pinned values may be literals, `RowRef`s, or (Python only) callables
taking the existing context object. A callable makes the scenario
non-serializable; `to_dict` raises and names the column.

Discovery mirrors pytest: `sqlproof scenarios` imports every
`*_scenarios.py` under the configured directory (default `scenarios/`)
and collects module-level `Scenario` objects.

## Serializable form (YAML / JSON)

The same model, without code. It is what gets stored and shared, the
MCP server accepts it, and a JSON Schema ships at
`src/sqlproof/schemas/scenario-v1.json`.

```yaml
version: 1
name: tiered_customers
seed: 42
tables:
  public.customers:
    - cohort: 2
      set: {tier: 1}
    - cohort: 3
      set: {tier: 2}
    - fill: 20
    - row: acme            # a named row: {ref: acme} elsewhere
      set: {tier: 1}
  public.invoices:
    - per_parent:
        parent: public.customers
        where: {tier: 1}
        count: [5, 10]
        default: [0, 3]
```

Unknown top-level keys are rejected, except under `x-*`, which tools
building on scenarios may use for their own settings.

### Phase 2 as implemented

- `Scenario.to_dict()` writes this form and `Scenario.from_dict()` reads
  it (`sqlproof.scenarios.serialize`). A named row is `{row: <name>}`,
  so in this form a column called `name` can be pinned on a named row.
  Unset `per_parent` options (`where`, `column`) are left out.
- Non-JSON pinned values are written as the strings Postgres reads:
  dates and times in ISO 8601, decimals and UUIDs as text. The
  fingerprint is computed from this same form, so a Python scenario and
  its export have the same fingerprint and load the same rows.
- Files: one scenario per `*.scenario.yaml`, `*.scenario.yml` or
  `*.scenario.json`, discovered next to `*_scenarios.py`; names are
  unique across both. YAML needs the `yaml` extra (PyYAML); JSON needs
  nothing.
- The parser checks every rule itself and names the path of what it
  rejects (`$.tables['customers'][1].cohort must be an integer`). The
  JSON Schema is for editors; it ships in the package and is served at
  `https://sqlproof.com/schemas/scenario-v1.json`, and a test keeps the
  two copies identical.
- `sqlproof scenarios export NAME [--format yaml|json] [--output PATH]`.
- A clock without a timezone is UTC, in Python and in files.

## Compilation

`Scenario.compile(schema)` validates the spec against the introspected
schema and produces a `CompiledScenario`:

- `sizes: dict[str, int]`, keyed by qualified name (see "Qualified
  sizes" below);
- `columns: dict[str, override]`, row-index overrides built from the
  cohort layout;
- `fk_layout: dict[str, ChildLayout]`, the child-index → parent-index
  map for every `per_parent`;
- `refs: dict[str, KeyValue]`, the resolved key of every named row;
- `fingerprint`: hash of the normalized spec plus the schema fingerprint.

**Cohort columns.** For each pinned column, one override:

```python
def override(ctx):
    group = layout.group_for(ctx.row_index)   # bisect over cohort start offsets
    return group.pinned.get(column, GENERATE)
```

**`GENERATE` sentinel (generator change).** Both generators gain one rule:
an override that returns `sqlproof.generators.GENERATE` means "generate
this value as if no override existed". In `rows.py` and `bulk.py` that
means building the column's sampler or refined strategy even when an
override is present (today both skip it, deliberately, because
`spec_for_column` can raise for an undimensioned `vector`). The sampler
is built lazily, on the first `GENERATE`, so that escape hatch still
works for columns that are always pinned. This is the only behaviour
change to existing code paths, and it is invisible to callers that never
return the sentinel.

**Per-parent layout.** The compiler knows the parent table's cohort
layout, so it knows exactly which parent indices match `where`. With the
scenario seed it draws `k_p` for every parent `p`, sets the child
table's size to `sum(k_p)`, and lays children out parent by parent. The
FK column's override is then
`_unique_value(referenced_pk, type, parent_index_for_child[row_index])`,
the same expression the bulk path already uses, so keys line up by
construction. `per_parent` and a pinned value on the same FK column is a
compile error.

**Validation, all before any database work:**

- unknown table or column; a pinned column that is generated or part of
  a single-column primary key;
- a pinned literal that violates a CHECK sqlproof can interpret, or a
  type it cannot cast;
- a pinned value repeated across more than one row of a UNIQUE column;
- a reference to an undefined or duplicate row name;
- a required FK whose parent table ends up with zero rows;
- an FK cycle (the bulk loader already refuses these and names them).

Each error names the scenario, the table and the group.

## Determinism contract

> Same normalized scenario, same seed, same schema fingerprint and same
> sqlproof version ⇒ byte-identical `COPY` input, and therefore identical
> rows.

What it takes:

1. **Volatile defaults are generated, not left to Postgres.** In a
   scenario load, a column whose default calls a volatile function
   (`now()`, `clock_timestamp()`, `random()`, `gen_random_uuid()`,
   `uuid_generate_v4()`; detected from the parsed default expression) is
   generated from the seed. Timestamps come from a fixed scenario clock
   (default `2026-01-01T00:00:00Z`, overridable per scenario). Stable
   defaults (literals, sequences) stay with Postgres; sequences are reset
   before the load so they replay identically.
2. **One generator path.** Scenarios always load through the bulk path.
   `Scenario.strategy()` (below) is for property tests and makes no
   byte-identity promise.
3. **Aligned value ranges.** The bulk and Hypothesis paths still draw
   different ranges for six types (scale follow-up 12). Not a blocker
   for byte identity, which only involves the bulk path, but it is for
   `strategy()` agreeing with `load()`. Land follow-up 12 before phase 3.
4. **A golden test per release.** A fixed schema plus scenario whose
   `COPY` bytes are checked in; any change to generated values fails CI
   and must be called out in the changelog as a data-changing release.

`LoadReport` records `sqlproof_version`, `schema_fingerprint`,
`scenario_fingerprint` and `seed`, so a consumer comparing two loads can
tell a code change from a generator change.

## Loading

`Scenario.load(dsn)` compiles, refuses non-empty target tables exactly as
the scale sweep does (including the row-level-security case fixed in
#110), loads with `load_dataset`, resets sequences, runs `ANALYZE` and
returns a `LoadReport` (rows per table, named-row keys, fingerprint).
`truncate_existing=True` is the same explicit opt-in.

External tables follow the existing `ExternalTableSpec` rules. For
Supabase, `auth.users` rows named in a scenario are inserted the way
`seed_test_users_directly` does today, and their ids become the named
rows' references.

CLI:

```
sqlproof scenarios list [--dir scenarios/]
sqlproof scenarios show NAME            # compiled sizes, refs, fingerprint
sqlproof scenarios up NAME --dsn DSN [--truncate-existing]
sqlproof scenarios export NAME          # serializable form as YAML
```

`load_dataset` becomes public as `sqlproof.load_dataset` in the same
release, since scenarios make it a supported path anyway.

## Integrations

- **Property tests.** `scenario.strategy(proof)` returns a Hypothesis
  strategy: pinned groups are fixed, fills draw from the existing
  strategies, and Hypothesis shrinks only the unpinned rows. A property
  can then say "for every dataset that contains this scenario's pinned
  rows".
- **Scale.** `scale_analysis(proof, fn, scenario=tiered_customers)` uses
  the scenario's compiled sizes as the base profile. At factor `k`,
  fills and per-parent counts scale by `k`; named rows and cohorts stay
  fixed by default (`scale_cohorts=True` scales them too). Function
  arguments can be references (`ref("acme")`), resolved at each factor.
- **MCP.** A `load_scenario` tool taking the serializable form and a
  DSN, and a `compile_scenario` tool that returns sizes and refs without
  touching a database. Agents describe states; they do not write seed
  SQL. `load_scenario` has no truncate option: it refuses tables that
  hold rows, and emptying them is left to a person. Its errors are
  scrubbed of the connection string and password. (Phase 2.)
- **Counterexamples.** A shrunk counterexample can be exported as a
  scenario (`sqlproof replay FILE --as-scenario`), so a failure can be
  reloaded as a database with `scenarios up`.

## Qualified sizes

Scenarios key tables by qualified name. `sizes=` today is keyed by bare
name and resolves `auth.users` through `ExternalTableSpec`. Compilation
emits qualified keys, and `load_dataset` and `dataset_strategy` learn to
accept them, preferring an exact qualified match and falling back to the
bare name, so existing callers are unaffected. Two tables with the same
bare name in different schemas then become loadable, which they are not
today.

## Phasing

| Phase | Ships | Depends on |
| --- | --- | --- |
| 1 | Builder API, compiler, `GENERATE`, per-parent layout, named rows, `Scenario.load`, `scenarios list/show/up`, public `load_dataset`, volatile-default generation, golden test | — |
| 2 | Serializable form + JSON Schema, `scenarios export`, loading YAML scenarios, MCP tools | 1 |
| 3 | `Scenario.strategy`, `scale_analysis(scenario=...)` | 1, scale follow-up 12 |
| 4 | Counterexample export | 1, counterexample reproducibility (seed is `None` today) |

The serializable form moves ahead of the property-test integration
because many Supabase projects are TypeScript-only: YAML scenarios are
how they would use this at all.

Phase 1 alone already answers the original request: "2 Tier 1 users, 3
Tier 2 users, the rest generated, the same every time".

### Phase 3 as implemented

- `Scenario.strategy(proof)` wraps `dataset_strategy` with the compiled
  sizes and overrides, so pinned values are fixed in every draw and
  Hypothesis shrinks only the rest. A scenario that seeds `auth.users`
  is refused: a dataset cannot carry rows for a table outside the schema.
- `scale_analysis(..., scenario=...)` (and `run_sweep`) take a scenario
  instead of `sizes`. Internally both become a per-factor load profile.
  `Scenario.scaled(k)` is the profile at factor `k` and
  `Scenario.minimal()` the calibration round.
- **Scaling rule, refined from the sketch above.** Multiplying every
  per-parent count by `k` while fill parents also multiply grows the
  children as `k^2`, which a first live test measured as an exponent of
  about 1.7 for a linear function. So per-parent counts grow only for
  parents that stay fixed (a named row, or cohorts unless
  `scale_cohorts`), and totals stay linear in `k`.
- `RowRef` arguments resolve to the named row's key at every factor and
  are recorded as `{"kind": "ref"}` in the artifact's `argument_policy`.
  The artifact gains a `scenario` entry (name, fingerprint,
  `scale_cohorts`).
- The scenario's seed is the sweep's (a conflicting `seed=` is refused),
  `columns=` cannot be combined with a scenario, and the scenario's
  `auth.users` rows are removed when the sweep ends.

### Phase 4 as implemented

- **Reproducibility.** `sqlproof(..., seed=)` now seeds Hypothesis
  (it was accepted and ignored). Without a seed, each run picks one at
  random; either way the counterexample's `seed` field records it, so
  the same seed repeats the same search. `sqlproof replay FILE` prints
  it.
- **`sqlproof replay FILE --as-scenario`** (`--schema-file` or `--dsn`,
  `--name`, `--format`, `--output`) builds the scenario in
  `sqlproof.scenarios.counterexample`. Each row becomes a pinned row:
  named `<table>_<n>` when its table has a single-column key, a
  one-row cohort otherwise. Every column the dataset holds is pinned
  except that key and generated columns. A single-column foreign key to
  a row in the dataset becomes a reference, and one to `auth.users`
  becomes a reference to a seeded auth user, so relationships survive
  the new keys. Keys are reassigned in row order; the counterexample
  file keeps the original numbers. The scenario is compiled before it
  is written, and a schema whose fingerprint differs from the recorded
  one is a warning, not a refusal.
- **Decision: refs, not pinned keys.** Pinning the original key values
  would need key pinning, which the compiler refuses because generated
  rows would collide with them. References keep the scenario valid at
  any seed and readable.
- What it needed elsewhere:
  - Serialized values may be lists (array columns) and objects (json
    columns). A json column pinned to a string used to load as a JSON
    string, not the document the earlier docs implied.
  - The columns of a *composite* primary key are pinnable (a join
    table's key is its references), and the bulk generator honours an
    override on them, as the Hypothesis path already did. Keeping the
    pairs distinct is then the scenario's job; a duplicate is a unique
    violation at load.
  - Counterexample files write `bytea` values as `\x...` (they failed
    to serialize).
  - The Hypothesis path drew a top-level `None` for json columns,
    which loads as SQL NULL and broke `NOT NULL json` columns. JSON
    null now appears only inside a document.
- Not covered: array columns are not generated yet (they parse as
  their element type; see the v0.1 scope in SPEC.md), so a
  counterexample never holds one. The exporter passes lists through
  for when they are.

### Phase 1 as implemented

Restrictions in phase 1, each a clear compile-time error:

- A table sized by `per_parent` cannot also have cohorts, named rows or
  fills, and a table takes one `per_parent` distribution. "Exclude a
  parent" is `per_parent(where=..., count=0, default=...)`; the
  `never_under` sketched in an earlier draft was dropped as redundant.
- `where=` as a mapping matches parent rows whose *cohort pins* those
  values. A fill row that happens to draw the same value gets
  `default=`.
- The only external table is `auth.users`, which takes `fill(n)` and
  named rows but no pinned columns.
- `row()` takes the row name as `name=`, so a column called `name`
  cannot be pinned on a named row; pin it with `cohort(1, name=...)`.

What `Scenario.load` adds beyond `load_dataset`: one transaction,
refusal of non-empty tables (RLS-hidden rows included) unless
`truncate_existing=True`, owned sequences reset before the load and set
past the generated keys after it, `auth.users` rows inserted with ids
derived from the scenario name, seed and index, then `ANALYZE`.

This phase also fixes a pre-existing bug in the dependency graph: a
table with two foreign keys into the same parent (`lender_id` and
`borrower_id`, both into `people`) was reported as a circular
dependency, because in-degree counted foreign keys while draining a
parent decremented once per dependent table.

## Testing strategy

- **Compiler, pure:** layouts, per-parent draws and validation errors
  over synthetic `SchemaInfo` objects. Property test: for random cohort
  lists, every row index maps to exactly one group, and group sizes sum
  to the table size.
- **Determinism:** load the same scenario twice into fresh databases and
  compare full table dumps; the golden `COPY` bytes test.
- **Live:** cohorts honoured (`count(*) WHERE tier = 1 = 2`), per-parent
  counts within bounds, refs resolve to real rows, volatile defaults
  identical across loads, RLS non-empty refusal.

## Open questions

1. **`now()` inside functions.** Volatile defaults are fixed at load
   time, but a function that calls `now()` at query time still sees the
   real clock. There is no supported way to freeze `now()` inside
   Postgres; anything that compares results across loads has to
   normalize timestamps or pass an explicit `as_of`.
2. **Scaling cohorts.** Keeping pinned cohorts fixed while fills grow is
   the right default for "does it still work for these two customers",
   but it changes the data's proportions as `k` grows. Worth confirming
   with a real sweep before phase 3.
3. **Supabase callers as named rows.** `auth.users` rows named in a
   scenario are seeded the way `seed_test_users_directly` does today.
   Whether `anon` and `service_role` need first-class handling in the
   scenario model, or stay with `as_rls_user`, is open.
