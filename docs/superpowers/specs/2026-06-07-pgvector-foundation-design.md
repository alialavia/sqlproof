# pgvector foundation

**Status:** approved (design phase)
**Date:** 2026-06-07
**Issue:** closes [#69](https://github.com/alialavia/sqlproof/issues/69)
**Sequence:** foundation; `contrib/pgvector` invariant helpers tracked separately.

## Summary

Recognise `vector(N)` columns end-to-end so a schema that uses pgvector
generates valid datasets through SqlProof without per-column overrides.
Closes the gap that forced the inbox sample to ship a
`vector_strategy(dim)` workaround.

## Scope

In:

- Generator support for `vector(N)`: emit a Postgres vector literal of
  the declared dimension whenever the schema declares a vector column.
- Introspection support: DSN-backed schemas recover the dimension from
  `pg_attribute.atttypmod` so the generator branch fires the same way.
- Sample cleanup: the inbox sample drops its `vector_strategy` helper.
- Tests covering parse → generate → INSERT → introspect.

Out (deliberate non-goals):

- `halfvec(N)`, `sparsevec(N)`, `bit(N)` binary quantisation.
- `contrib/pgvector` module and distance-aware strategies
  (`vector_far_from`, `vector_near`, etc.) — separate spec.
- Distance-aware CHECK refinement.
- Function-signature parsing for `CREATE FUNCTION foo(... vector(N) ...)`
  arguments (already silently tolerated; generator is unaffected).
- Index-aware planning for HNSW / IVFFlat.
- The broader DSN-introspection modifier gap on `varchar(n)` /
  `numeric(p,s)` (file a separate tracking issue).

## Success criteria

1. A schema declaring `embedding vector(N) NOT NULL` round-trips
   through parse → generate → INSERT → query with no override.
2. The same holds when the schema is loaded via
   `SqlProof.from_connection_string(...)`.
3. The inbox sample's `tests/_helpers.py` is deleted, the
   `"message_embeddings.embedding": vector_strategy(384)` override is
   removed from every test that uses it, and the existing recipe bugs
   continue to surface.
4. Issue #69 closes.

## Architecture

The change is additive across four touchpoints. No new public types,
no schema-model changes.

| Touchpoint | Change |
| --- | --- |
| `schema/parse_sql.py` | None. `vector(384)` already parses to `PgType(kind="scalar", name="vector", modifiers=(384,))`. |
| `generators/columns.py` | New `vector` branch in `strategy_for_type`. |
| `schema/introspect.py` | `_COLUMNS_SQL` decodes `atttypmod` for vector columns only. |
| `types.py` | None. `generate_types` already emits `object` per column. |

## Design

### Generator branch

Insert into `strategy_for_type` (`generators/columns.py`), before the
final text fallback:

```python
if name == "vector":
    if not pg_type.modifiers:
        raise SqlProofSchemaError(
            "vector type requires a dimension (e.g. vector(384)); "
            "got vector with no modifier"
        )
    dim = pg_type.modifiers[0]
    # Bounded integers + scale-to-float rather than st.floats:
    # Hypothesis's bounded-float draws use ~3x the entropy per value
    # of bounded integers, and exhaust the default 8KB conjecture
    # buffer at common embedding sizes (1536, 2000). Integers also
    # shrink toward 0, so end-state is still an all-zeros vector.
    component = st.integers(min_value=-1_000_000, max_value=1_000_000)
    return (
        st.lists(component, min_size=dim, max_size=dim)
        .map(
            lambda xs: "["
            + ",".join(f"{x / 1_000_000:.6f}" for x in xs)
            + "]"
        )
    )
```

Rationale:

- Component values come from bounded integers in `[-1_000_000,
  1_000_000]` scaled to `[-1, 1]` via `/ 1_000_000`. The earlier
  draft proposed `st.floats(width=32, ...)` to match pgvector's
  float32 storage, but Hypothesis's bounded-float strategy exhausts
  the default 8KB conjecture buffer at dim ≥ ~1100 — fatal for
  realistic embedding sizes like OpenAI's `text-embedding-3-small`
  (1536) and `text-embedding-3-large` (3072). Integers carry less
  per-draw entropy, fit comfortably at 2000+ dims, and preserve the
  shrink-toward-zero semantics the spec depends on (integer 0 maps
  to `0.000000`).
- 6-decimal-digit resolution per component (`f"{x / 1_000_000:.6f}"`)
  is well within pgvector's float32 storage precision; no meaningful
  loss for property tests.
- The strategy emits a string, INSERTed verbatim. No new dependency on
  the `pgvector` Python package; works with bare `psycopg`.
- Component range `[-1, 1]` keeps L2 distances bounded and cosine
  well-conditioned, matching the existing workaround so behavioural
  changes in the inbox sample are limited to "where the vectors come
  from." This is a test-input convention (normalised embeddings),
  not a pgvector constraint — pgvector itself accepts any float32.
- `±inf` and `NaN` aren't representable through the integer scale —
  same exclusion the float-based draft enforced via `allow_nan=False,
  allow_infinity=False`, but achieved structurally.

### Shrinking behaviour (documented in the docstring on the branch)

- Dimension is fixed (`min_size == max_size == N`); shrinking never
  changes vector length.
- Each component shrinks toward `0.0`; a fully-shrunk vector is N
  zeros.
- Zeros are the right shrink target for dataset-shape bugs
  (RLS leaks, JOIN errors, pagination ties) because they remove vector
  content as a confounder in the counterexample.
- Distance-sensitive invariants (top-k monotonicity, similarity
  ordering) want non-zero norms — those land in the upcoming
  `contrib/pgvector` spec, which will provide knobs like
  `allow_zero=False` and `vector_far_from(...)`.

### Introspection

`_COLUMNS_SQL` (`schema/introspect.py`) currently hardcodes
`ARRAY[]::integer[] AS modifiers`. For vector columns the dimension is
stored verbatim in `pg_attribute.atttypmod` (no offset trick — unlike
`varchar`, where `atttypmod = length + 4`). Scoped fix:

```sql
CASE
  WHEN typ.typname = 'vector' AND att.atttypmod > 0
    THEN ARRAY[att.atttypmod]
  ELSE ARRAY[]::integer[]
END AS modifiers
```

Notes:

- `atttypmod = -1` means "no modifier specified" — for a vector column
  that means `vector` without dimension. We do not fabricate a
  dimension; the generator path raises the same error as the
  file-parse path.
- The condition is namespace-agnostic: pgvector installed under a
  non-default schema (e.g. Supabase's `extensions` schema) still has
  `typname = 'vector'`. A consequence is that a hypothetical
  user-defined `vector` type in some other schema would be treated
  the same way — vanishingly rare in practice and accepted as the
  cost of the simple match.
- The broader modifier gap (`varchar(n)`, `numeric(p,s)` losing
  precision via the DSN path) is real but out of scope. Filed as a
  separate tracking issue.

## Testing

### Unit

- `tests/test_parse_sql.py` — assert `vector(384)` parses to
  `PgType(kind="scalar", name="vector", modifiers=(384,))`. May already
  pass; the test pins the contract explicitly.
- `tests/test_generators_columns.py` — property test: for
  `N ∈ {1, 4, 384, 1536, 2000}`, the generated string is a valid
  pgvector literal containing exactly N components, each in `[-1, 1]`.
- `tests/test_generators_columns.py` — error case:
  `PgType(kind="scalar", name="vector", modifiers=())` raises
  `SqlProofSchemaError` with a message that mentions the `vector`
  type and the missing dimension. (Column name is not in scope at
  `strategy_for_type`; the row generator wraps the error with column
  context up the stack.)

### Integration (gated on `SQLPROOF_TEST_DATABASE_URL`)

- `tests/integration/test_pgvector.py` — `CREATE EXTENSION vector`,
  declare a `vector(8)` column, generate 10 rows via `row_strategy`,
  INSERT them, assert `SELECT count(*) = 10`.
- `tests/integration/test_introspect.py` — DSN containing a
  `vector(16)` column round-trips through `introspect_schema` and
  produces `Column.type.modifiers == (16,)`.

Both integration tests skip gracefully when the `vector` extension
is not installable on the target server.

### Sample-level

- Delete `examples/inbox/tests/_helpers.py` entirely (sole export is
  `vector_strategy`).
- Strip
  `"message_embeddings.embedding": vector_strategy(384)` overrides
  from `test_similar_tickets.py` and `test_hybrid_search.py`.
- Drop the issue-#69 mention from affected docstrings.
- Run the full inbox suite; confirm each recipe's intentional bug
  still surfaces. The dataset shape will change (generator-supplied
  vectors rather than user-supplied), so individual failure traces
  will differ; the property-failure counts should not.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Inbox failure traces shift in unexpected ways | Diff the suite output before/after; if a recipe stops failing, that is the new bug and must be tracked. |
| `width=32` floats produce literals psycopg or pgvector parses inconsistently | Round-trip property test against a live database covers this. |
| Vector dimension in `atttypmod` decoding has edge cases on older pgvector versions | Test on at least the pgvector version shipped with the inbox sample's Supabase image; document the minimum. |
| `contrib/pgvector` spec slips and users want `vector_far_from` semantics today | Foundation is independently useful; users can write inline strategies as escape hatch. |

## PR housekeeping

- Closes [#69](https://github.com/alialavia/sqlproof/issues/69).
- File a tracking issue for the broader DSN-introspection modifier
  gap (`varchar(n)`, `numeric(p,s)`).
- File a tracking issue for the `contrib/pgvector` follow-up spec
  (`vector_strategy` knobs, `vector_far_from`, `vector_near`,
  invariant helpers).
