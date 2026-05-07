# Supabase RLS — pgTAP vs property-based testing

A side-by-side translation of the [official Supabase RLS testing
example](https://supabase.com/docs/guides/database/postgres/row-level-security#testing-policies-with-pgtap)
into SqlProof's property-based idiom.

## What's tested

The schema (`schema.sql`) is a trimmed version of the orgs / org_members /
posts schema in the Supabase docs:

- Per-org role hierarchy: `owner > admin > editor > viewer`.
- Posts have a complex 3-branch visibility policy.
- Free-plan organizations cap at `max_posts` posts.
- Member management is gated to owner/admin only.

The tests (`test_rls.py`) assert the *invariants* underneath those
policies — not specific (user, post, count) tuples.

## Run

```bash
export SQLPROOF_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres
uv run pytest examples/supabase_rls
```

The example sets up its own schema and a seed pool of `auth.users` rows
on first import. Re-runs are idempotent — the `DROP TABLE` /
`DROP SCHEMA` calls reset the example's tables without touching anything
else in the database.

## Comparison

|                                | pgTAP version | sqlproof version |
| ------------------------------ | ------------- | ---------------- |
| Lines of code                  | ~220          | ~140             |
| Hardcoded data values          | ~30           | 0                |
| Test scenarios per run         | 10            | ~150 (user × post × role) per run × 30 runs + boundary points + actor-role variations |
| Boundary values for `max_posts`| 1             | 0, 1, 2, 3, 4    |
| Cross-org visibility tested    | partially     | exhaustively     |
| Multi-membership per user      | not tested    | falls out of generation |
| Off-by-one in plan limits      | undetected    | caught at boundary |
| Role × post-state combinations | partial (4×3) | exhaustive       |

The qualitative difference: the pgTAP version asserts "premium_user
sees 3 posts in premium-org" — a single point on the policy. The
property-based version asserts "for any (user, post) the live policy's
result equals the Python model's" — the whole policy.

## Property 4 side-by-side

For a concrete sense of where the size difference goes,
[`test_rls.pgtap.sql`](./test_rls.pgtap.sql) ports a single property —
`test_outsider_cannot_read_drafts_in_another_org` (Property 4 in
[`test_rls.py`](./test_rls.py)) — into idiomatic pgTAP. Same property,
two implementations:

|                                       | pgTAP                  | sqlproof                       |
| ------------------------------------- | ---------------------- | ------------------------------ |
| Lines                                 | 177                    | ~50                            |
| Test cases per run                    | 12 (hand-written)      | 20 (randomized)                |
| `(member_role × is_premium)` coverage | hand-written 3 × 2     | implicit via Hypothesis        |
| UUIDs                                 | hardcoded              | generated                      |
| Auth context per transition           | 4 lines × 4 sites      | one `with as_rls_user(...)`    |
| Sanity assertions                     | required               | not needed (Property 1 covers) |

What drives the verbosity:

1. **No data generation.** Every UUID and column literal is in the
   file. With sqlproof, `sizes` and `columns` describe the dataset
   shape and the generator fills it in.
2. **Property coverage by enumeration.** The property holds for any
   `(member_role, is_premium)` pair, so all six cells of the matrix
   are written out. sqlproof's randomized runs cover the matrix for
   free.
3. **Auth-context dance is inline at every transition.**
   `set_config` + `SET LOCAL ROLE` + `RESET ROLE` repeats four times
   in the pgTAP file. `as_rls_user` collapses each occurrence to one
   line.
4. **Sanity assertions are mandatory.** pgTAP doesn't compare against
   a model, so you have to explicitly assert "the legitimate member
   CAN see the draft" — otherwise an over-restrictive RLS policy that
   hides everything would pass the negative checks for the wrong
   reason. Property 1 already covers the positive case via the
   visibility model, so Property 4 doesn't repeat it.

## How it stays this short

Three small libraries doing the work for you:

1. **`@sqlproof(proof, sizes=..., columns=..., runs=N)`** decorator. Generates
   datasets that respect every FK / CHECK / UNIQUE in the schema, runs the
   property `N` times with shrinking on failure, passes the live `db` and
   the generated `dataset` to your function. No `_insert_user` /
   `_insert_post` helpers — the generator already produces valid rows.
2. **`ExternalTableSpec`** for `auth.users`. Tells the generator "don't
   generate users, sample from the existing seeded pool." The example
   seeds 5 users at module import; tests pull from that pool for FK
   targets.
3. **`as_rls_user(db, user_id)`** from `sqlproof.contrib.supabase` —
   context manager that switches the connection to the `authenticated`
   role *and* sets the JWT-claim GUC so `auth.uid()` returns the right
   value. Setting one without the other silently bypasses RLS, so the
   helper makes the right thing easy.

## What this example doesn't show

- **Time-based publishing.** The original Supabase example tests a
  `scheduled_for` column with `tests.freeze_time(...)`. SqlProof can
  generate timestamps over a range and assert against `now()`-based
  filters; not in this example to keep it focused on the RLS policy
  shape.
- **Comments.** The original schema includes a `comments` table; we
  trimmed it to keep the schema readable.
- **Profile rows.** The original creates `profiles` per user. They're
  not load-bearing for the RLS policies we test, so they're skipped.
- **Stateful tests.** A `MembershipChurnMachine` that randomly adds /
  removes / re-roles members and asserts visibility after every
  operation would catch class-of-bug-#5 from the README. Not in this
  example; see `tests/unit/test_state_machine.py` for the stateful
  pattern.
