# Mutation testing for RLS policies (`MutationSet.for_policy`)

**Status:** brainstorming in progress (2026-09-15). The scope decision is
settled by the user. The approach is proposed but **not yet approved** — do
not start implementing before the user approves the approach and the design
sections below.

**Why this file exists:** handoff from a local Claude Code session so the work
can continue elsewhere. Everything a fresh session needs is here; nothing else
from that conversation is required.

**Related:** `docs/superpowers/specs/2026-06-10-mutation-testing-design.md` —
the v1 mutation design, which names `MutationSet.for_policy(...)` as the RLS
analogue and calls RLS the headline use case for the Supabase audience.

## Goal

Mutate RLS policies directly. Today the harness mutates `CREATE FUNCTION`
bodies only (`target_kind: Literal["function"]`, `model.py:89`; extraction
filters on `CreatePolicyStmt`'s sibling `CreateFunctionStmt`,
`extract.py:36`). The documented workaround is to mutate the helper function a
policy calls, which does nothing for a policy whose condition is written
inline — the common Supabase shape.

Three docs pages currently promise this feature and must be updated when it
lands:

- `website/src/content/docs/api/mutation-testing.md:179`
- `website/src/content/docs/guides/mutation-testing.md:174`
- `website/src/content/docs/examples/inbox/mutation-scoring.md:142`

## Settled: what a policy mutant may change

**The whole `CREATE POLICY` statement**, not only the `USING` / `WITH CHECK`
conditions. The user chose this over a conditions-only design.

It buys mutations that conditions-only cannot express:

- roles: `TO authenticated` → `TO public`
- command: `FOR UPDATE` → `FOR ALL`
- `AS RESTRICTIVE` → `AS PERMISSIVE`
- deleting the `WITH CHECK` clause outright — which is exactly the bug in
  inbox recipe 9 (`examples/inbox/schema/011_fix_org_members_with_check.sql`)

`ALTER POLICY` can only change `USING`, `WITH CHECK` and roles; it cannot
change the command, the permissive flag, or remove a `WITH CHECK` clause. So
whole-statement scope implies applying mutants as `DROP POLICY` +
`CREATE POLICY`, which is fine because every mutant runs on a throwaway clone.

## Proposed approach (awaiting approval)

**A — text edits on the policy exactly as written in the schema file.**

1. Locate the `CREATE POLICY` in the schema file with pglast, matching on
   table + policy name.
2. Cut its source text out using pglast's tokenizer: from the statement's
   `CREATE` token to the next `;` token. (Not `stmt_len` — see findings.)
3. Apply the existing `Replace` / `Drop` ops to that text, each matching
   exactly once, as for function bodies.
4. Re-parse the mutated text and require exactly one statement, a
   `CreatePolicyStmt`, with the same table and name.
5. On the clone, run `DROP POLICY <name> ON <table>; <mutated CREATE POLICY>`
   as one query.

Authors — and LLMs emitting the JSON mutant format — write patterns against
what they see in their own migration file, exactly as they do for functions.

**Rejected alternatives.**

- **B — edit pglast's re-rendered SQL.** No source slicing needed, and
  whitespace-insensitive, but patterns would have to match pglast's own
  formatting (`FOR select`, `TRUE`, collapsed to one line) rather than the
  user's file. Unusable for hand-written or LLM-written patterns.
- **C — structured ops** (`SetRoles`, `SetCommand`, `DropWithCheck`).
  Precise, but adds new vocabulary to the JSON format and the docs, and
  contradicts the v1 principle of text-level authoring with AST-level
  validation. Text edits already cover every case.

## Verified findings

Probed locally against this repo, pglast 7.13 and psycopg 3.3.3.

- **pglast parses policies fully.** `CreatePolicyStmt` exposes
  `policy_name`, `table` (a `RangeVar` with `schemaname` / `relname`),
  `cmd_name`, `permissive`, `roles`, `qual`, `with_check`. `RawStream`
  deparses it back to valid SQL (`... AS PERMISSIVE FOR select TO
  authenticated USING (...) WITH CHECK (TRUE)`; lowercase `select` is fine,
  keywords are case-insensitive). `AlterPolicyStmt` and `DropStmt` also parse.
- **Node reprs are formatting-stable.** The same policy written with
  different whitespace produces identical `repr(stmt)`. So policies can reuse
  the function path's trick: AST text as the no-op check and as the mutant id
  input.
- **`stmt_len` is unreliable; tokenizer offsets are not.** In
  `examples/inbox/schema/003_fix_tickets_rls.sql`, whose comments contain two
  em dashes, pglast reports `stmt_len` 238 where the statement is 240
  characters. Replacing the em dashes with ASCII gives the correct 240. All
  offsets (`stmt_location`, token `start`/`end`) are character offsets into
  the Python string and are correct; only the length is off. Slicing from the
  `CREATE` token to the next `;` token was verified correct, including with
  non-ASCII inside the statement itself, and the slice re-parses. Token `end`
  is inclusive. `stmt_len` is currently unused in `src/`.
- **A two-statement DDL string works.** psycopg sends a parameterless query
  with the simple protocol (`psycopg/_cursor_base.py:456-459`), which permits
  several statements in one `execute`. Postgres runs such a query as one
  implicit transaction, so a failing `CREATE` rolls the `DROP` back. The
  runner's `_apply_ddl` (`runner.py:175`) needs no change.
- **Reporting groups on a plain string.** `MutantOutcome.target` is a `str`
  (`result.py:20`) and the dashboard groups by it (`aggregate.py:155`). The
  artifact is `schema_version` 1 and reads `duration_s` with `.get`
  (`artifact.py:86`) — the precedent for adding a field without a version bump.
- **The JSON format already anticipates this.** A mutant serializes as
  `{"target": {"kind": "function", "name": ...}, "ops": [...]}` and
  `from_dict` rejects any other kind (`model.py:124`).
- **Mutant ids must stay stable for functions.** Today's digest is
  `sha256(f"{target_name}\n{mutated_key}")[:16]` (`apply.py:131`), pinned by
  `tests/unit/test_mutation_id_stability.py`. Policy ids must therefore use a
  distinct input rather than changing the function scheme.
- **Real migrations use `DROP POLICY IF EXISTS ...; CREATE POLICY ...`.**
  See the inbox schema files. A single migration file holds one `CREATE
  POLICY` per policy; a concatenation of all migrations holds several.

## Proposed design details (not yet approved)

### API

```python
mutations = (
    MutationSet.for_policy("org_members", "members manage their own row", [
        Drop(" AND org_members.role = 'viewer'"),
        Replace("TO authenticated", "TO public"),
        Replace("FOR UPDATE", "FOR ALL"),
    ])
    + MutationSet.for_function("is_admin_in_org", [...])
)
```

The table is required, and comes first so it reads like `ON org_members`.
Policy names are only unique per table, and reused names across tables are
common (Supabase's dashboard templates, e.g. "Enable read access for all
users"). Accept either `"tickets"` or `"public.tickets"`: match `relname`,
plus `schemaname` when given; ambiguity across schemas is an error.

### Model and JSON

`Mutant` gains `target_kind: Literal["function", "policy"]` and
`target_table: str | None = None` (required when the kind is `policy`,
rejected otherwise). Serialized as
`{"target": {"kind": "policy", "table": "org_members", "name": "..."}}`.
The function form is unchanged.

### Locating the policy

Exactly one `CREATE POLICY` for that table + name in the schema file, the same
rule functions already follow. `DROP POLICY` statements are not counted, so
the normal migration idiom works. Zero matches and more than one match are
both authoring errors. An `ALTER POLICY` for the same target anywhere in the
file is also an error, because the `CREATE` text would no longer be the
effective definition.

### Validation, all before any database work

1. Each op applies exactly once to the sliced statement text (existing
   `apply_op`).
2. The mutated text parses to exactly one statement, a `CreatePolicyStmt`,
   with the same table and name. This blocks injected extra statements,
   renames, and retargeting to another table.
3. Mutated AST equal to the original's is rejected as a no-op.
4. A digest equal to another mutant's is rejected as a duplicate.

### Identity and reporting

Policy digest: `sha256(f"policy\n{table}\n{name}\n{mutated_key}")[:16]`, where
`mutated_key` is `repr` of the mutated statement node. Function digests keep
their current input, so existing ids don't move. Clone naming
(`sqlproof_mutant_<id>`) is unchanged.

Outcome label for a policy: `"<policy name>" on <table>`. That keeps
`MutantOutcome.target` a string and the dashboard's grouping unchanged. The
alternative — a real `target_kind` field on the outcome and the artifact, read
with `.get` for old files — is deferred unless the dashboard wants a badge.

### Applying on the clone

`PreparedMutant.ddl` becomes `DROP POLICY "<name>" ON <table>;` followed by
the deparsed mutated `CREATE POLICY`. Deparsing rather than replaying the
mutated text guarantees the applied statement is the one that was validated.

## Open questions for the user

1. Real `target_kind` field on outcomes and artifacts, or the formatted target
   string above?
2. Keep "exactly one definition in the file", or allow last-definition-wins so
   `schema_file` can be a concatenation of migrations? Safe for policies, since
   table + name is a true identity; not safe for functions, which have
   overloads.
3. Which inbox recipes get policy mutants? Candidates: 2 (drop the org
   correlation in the tickets policy), 5 (drop the internal-message gate), 9
   (drop the `WITH CHECK` role pin), 10 (weaken the admin branch of the DELETE
   policy).
4. Mutating `ALTER TABLE ... ENABLE / FORCE ROW LEVEL SECURITY` — RLS switched
   off entirely — is a separate target kind. Out of scope here; worth filing?

## Impact map

| File | Change |
| --- | --- |
| `src/sqlproof/mutation/model.py` | target fields, `for_policy`, JSON round-trip |
| `src/sqlproof/mutation/extract.py` | locate policy, token-based source slice, build DDL |
| `src/sqlproof/mutation/apply.py` | per-kind keys and validation, policy digest |
| `src/sqlproof/mutation/runner.py` | expected unchanged |
| `tests/unit/test_mutation_{model,extract,apply,id_stability}.py` | policy cases |
| `tests/integration/test_mutation_live.py` | live policy mutant: one killed, one survivor |
| `examples/inbox/tests/mutation/test_mutation_inbox.py` | policy mutants per recipe |
| the three docs pages listed at the top | drop "planned", document `for_policy` |

## Testing notes

- Unit tests need no database.
- Live tests read `SQLPROOF_TEST_DATABASE_URL`; CI uses
  `postgresql://postgres:postgres@127.0.0.1:5432/postgres` against the
  supabase/postgres image, which has `CREATEDB`. Locally, see CONTRIBUTING for
  the `sqlproof-pg` container recipe.
- RLS only engages for a role that cannot bypass it. Follow
  `src/sqlproof/contrib/supabase.py:86` (`SET LOCAL ROLE` plus
  `request.jwt.claims`) and `tests/integration/test_supabase_auth_surface.py:70`
  (`set_config('request.jwt.claim.sub', ...)`, singular).
- The inbox mutation tests are manual, not part of CI: marker `mutation` plus a
  prepared `SQLPROOF_TEMPLATE_URL` (recipe in that module's docstring).

## How to continue

1. Get the user's approval on the approach above, then walk the design
   sections with them one at a time.
2. Fold the agreed design into this file as the finished spec and commit it.
3. Only then write the implementation plan (superpowers `writing-plans`), and
   implement test-first, as the rest of this repo does.

## Unrelated work in flight — leave alone

PRs #109 and #110 (scale measurement) are deliberately on hold; don't merge
them, and don't file the 15 issue drafts in
`docs/superpowers/follow-ups/2026-09-10-scale-measurement-follow-ups.md`
without asking.
