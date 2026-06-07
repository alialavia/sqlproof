# pgvector Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recognise `vector(N)` columns end-to-end so SqlProof generates valid datasets for pgvector schemas without per-column overrides.

**Architecture:** Additive change across three touchpoints — the column-generator gets a `vector` branch, the introspection SQL decodes `atttypmod` for vector columns, and the parser already does the right thing (verified by a pinning test). No public type changes, no new dependencies.

**Tech Stack:** Python 3.11+, Hypothesis, psycopg, pglast, pgvector ≥ 0.5 on the Postgres side. Existing test infra: `pytest`, `tests/unit/`, `tests/integration/test_*_live.py` for DSN-gated tests, integration tests skip via `SQLPROOF_TEST_DATABASE_URL` env var.

**Spec:** [`docs/superpowers/specs/2026-06-07-pgvector-foundation-design.md`](../specs/2026-06-07-pgvector-foundation-design.md)

**Branch:** `feat/pgvector-foundation` (off `main`).

---

## File structure

| File | Action | Responsibility |
| --- | --- | --- |
| `src/sqlproof/generators/columns.py` | Modify | Add `vector` branch in `strategy_for_type` that emits Postgres vector literals of the declared dimension. |
| `src/sqlproof/schema/introspect.py` | Modify | `_COLUMNS_SQL` decodes `atttypmod` for vector columns only, leaving the rest of the modifier path untouched. |
| `tests/unit/test_column_strategies.py` | Modify | Add three test functions covering the vector branch and the missing-dimension error. |
| `tests/unit/test_parse_sql_edge_cases.py` | Modify | Pin the parser's existing-but-undocumented behaviour: `vector(384)` → `PgType(kind="scalar", name="vector", modifiers=(384,))`. |
| `tests/integration/test_pgvector_live.py` | Create | Live-Postgres round trip: parse → generate → INSERT → introspect, gated on `SQLPROOF_TEST_DATABASE_URL` and `CREATE EXTENSION vector`. |
| `examples/inbox/tests/_helpers.py` | Delete (rebase-gated) | Sole export `vector_strategy` becomes redundant once the generator branch is live. |
| `examples/inbox/tests/test_similar_tickets.py` | Modify (rebase-gated) | Drop the `"message_embeddings.embedding": vector_strategy(384)` override and the `_helpers` import. |
| `examples/inbox/tests/test_hybrid_search.py` | Modify (rebase-gated) | Same cleanup. |

"Rebase-gated" = `feat/pgvector-foundation` was branched off `main`, but `examples/inbox/` only exists on `feat/inbox-sample`. Task 5 cannot run until this branch is rebased onto `main` after the inbox PR merges (or rebased directly onto `feat/inbox-sample`).

---

## Task 1: Vector branch in `strategy_for_type`

**Files:**
- Modify: `src/sqlproof/generators/columns.py:27-111`
- Test: `tests/unit/test_column_strategies.py`

- [ ] **Step 1.1: Write the happy-path failing test**

In `tests/unit/test_column_strategies.py`, add `import re` and `import math` near the other stdlib imports at the top of the file. Then append to the bottom:

```python
_VECTOR_LITERAL_RE = re.compile(r"^\[(.+)\]$")


@NON_NULL_KW
@given(data=st.data())
def test_vector_strategy_yields_literal_of_declared_dimension(data) -> None:
    pg = _scalar("vector", modifiers=(8,))
    value = data.draw(strategy_for_type(pg))
    assert isinstance(value, str)
    match = _VECTOR_LITERAL_RE.match(value)
    assert match is not None, f"not a pgvector literal: {value!r}"
    components = [float(part) for part in match.group(1).split(",")]
    assert len(components) == 8
    for component in components:
        assert -1.0 <= component <= 1.0
        assert not math.isnan(component)
        assert not math.isinf(component)


@pytest.mark.parametrize("dim", [1, 4, 384, 1536, 2000])
@NON_NULL_KW
@given(data=st.data())
def test_vector_strategy_holds_dimension_across_sizes(data, dim) -> None:
    pg = _scalar("vector", modifiers=(dim,))
    value = data.draw(strategy_for_type(pg))
    match = _VECTOR_LITERAL_RE.match(value)
    assert match is not None
    assert len(match.group(1).split(",")) == dim
```

- [ ] **Step 1.2: Run the new tests to confirm they fail**

Run: `uv run pytest tests/unit/test_column_strategies.py::test_vector_strategy_yields_literal_of_declared_dimension tests/unit/test_column_strategies.py::test_vector_strategy_holds_dimension_across_sizes -v`

Expected: both FAIL. Today `strategy_for_type` falls through to `_postgres_text(max_size=255)` for unknown scalar names, so the regex won't match.

- [ ] **Step 1.3: Add the vector branch**

In `src/sqlproof/generators/columns.py`, locate the final fallback `return _postgres_text(max_size=255)` at the bottom of `strategy_for_type`. Insert this branch *immediately before* it:

```python
    if name == "vector":
        if not pg_type.modifiers:
            raise SqlProofSchemaError(
                "vector type requires a dimension (e.g. vector(384)); "
                "got vector with no modifier"
            )
        dim = pg_type.modifiers[0]
        component = st.floats(
            min_value=-1.0,
            max_value=1.0,
            allow_nan=False,
            allow_infinity=False,
            width=32,
        )
        return (
            st.lists(component, min_size=dim, max_size=dim)
            .map(lambda xs: "[" + ",".join(repr(x) for x in xs) + "]")
        )
```

Add the import at the top of the file if not present:

```python
from sqlproof.exceptions import SqlProofSchemaError
```

- [ ] **Step 1.4: Run the happy-path tests; confirm they pass**

Run: `uv run pytest tests/unit/test_column_strategies.py::test_vector_strategy_yields_literal_of_declared_dimension tests/unit/test_column_strategies.py::test_vector_strategy_holds_dimension_across_sizes -v`

Expected: PASS.

- [ ] **Step 1.5: Write the error-case test**

Append to `tests/unit/test_column_strategies.py`:

```python
def test_vector_without_dimension_raises_schema_error() -> None:
    from sqlproof.exceptions import SqlProofSchemaError

    pg = _scalar("vector", modifiers=())
    with pytest.raises(SqlProofSchemaError) as excinfo:
        strategy_for_type(pg)
    assert "vector" in str(excinfo.value)
    assert "dimension" in str(excinfo.value)
```

- [ ] **Step 1.6: Run the error-case test; confirm it passes**

Run: `uv run pytest tests/unit/test_column_strategies.py::test_vector_without_dimension_raises_schema_error -v`

Expected: PASS.

- [ ] **Step 1.7: Run lint and types over the touched files**

Run: `uv run ruff check src/sqlproof/generators/columns.py tests/unit/test_column_strategies.py && uv run pyright src/sqlproof/generators/columns.py`

Expected: clean.

- [ ] **Step 1.8: Commit**

```bash
git add src/sqlproof/generators/columns.py tests/unit/test_column_strategies.py
git commit -m "$(cat <<'EOF'
feat(generators): emit pgvector literal for vector(N) columns

strategy_for_type now recognises the `vector` scalar with a single
modifier and emits a Postgres vector literal string of the declared
dimension. The text fallback previously produced random strings that
Postgres rejected at INSERT time, forcing per-column overrides in
schemas that use pgvector.

Refs #69

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Pin parser behaviour for `vector(N)`

The parser already produces the right `PgType` for `vector(N)`. The pinning test exists to fail loudly if a future refactor changes the contract.

**Files:**
- Test: `tests/unit/test_parse_sql_edge_cases.py`

- [ ] **Step 2.1: Write the pinning test**

Append to `tests/unit/test_parse_sql_edge_cases.py`:

```python
def test_vector_typed_column_parses_with_dimension_in_modifiers() -> None:
    from sqlproof.schema.parse_sql import parse_schema_sql

    sql = """
    CREATE TABLE embeddings (
        id serial PRIMARY KEY,
        embedding vector(384) NOT NULL
    );
    """
    schema = parse_schema_sql(sql)
    column = schema.table("embeddings").column("embedding")
    assert column.type.kind == "scalar"
    assert column.type.name == "vector"
    assert column.type.modifiers == (384,)
    assert column.nullable is False
```

- [ ] **Step 2.2: Run the test; confirm it passes**

Run: `uv run pytest tests/unit/test_parse_sql_edge_cases.py::test_vector_typed_column_parses_with_dimension_in_modifiers -v`

Expected: PASS (this pins existing behaviour).

- [ ] **Step 2.3: Lint**

Run: `uv run ruff check tests/unit/test_parse_sql_edge_cases.py`

Expected: clean.

- [ ] **Step 2.4: Commit**

```bash
git add tests/unit/test_parse_sql_edge_cases.py
git commit -m "$(cat <<'EOF'
test(parse_sql): pin vector(N) parsing contract

vector(N) already parses to PgType(kind="scalar", name="vector",
modifiers=(N,)). Pin this so a future refactor of _parse_type_node
can't silently drop the modifier and break the new generator branch.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Introspect dimension from `atttypmod`

**Files:**
- Modify: `src/sqlproof/schema/introspect.py:265-286` (`_COLUMNS_SQL`)
- Test: covered by the integration test in Task 4 (the SQL change is verifiable only against a live Postgres with pgvector).

- [ ] **Step 3.1: Update `_COLUMNS_SQL`**

In `src/sqlproof/schema/introspect.py`, find the `modifiers` projection in `_COLUMNS_SQL`. It currently reads:

```sql
  ARRAY[]::integer[] AS modifiers
```

Replace it with:

```sql
  CASE
    WHEN typ.typname = 'vector' AND att.atttypmod > 0
      THEN ARRAY[att.atttypmod]
    ELSE ARRAY[]::integer[]
  END AS modifiers
```

Leave the surrounding query (joins, WHERE clause, ORDER BY) unchanged.

- [ ] **Step 3.2: Lint and type-check**

Run: `uv run ruff check src/sqlproof/schema/introspect.py && uv run pyright src/sqlproof/schema/introspect.py`

Expected: clean (the SQL is a multi-line string; no Python-side change).

- [ ] **Step 3.3: Run the unit suite to confirm no regression**

Run: `uv run pytest tests/unit -q`

Expected: all PASS (the change is SQL-only and unit tests don't hit Postgres).

- [ ] **Step 3.4: Commit**

```bash
git add src/sqlproof/schema/introspect.py
git commit -m "$(cat <<'EOF'
feat(introspect): decode dimension from atttypmod for vector columns

For vector columns, atttypmod stores the declared dimension verbatim
(no -4 offset like varchar). Surface it in the modifiers tuple so DSN-
backed schemas hit the same generator branch as file-parsed schemas.

Scoped to vector to avoid mixing in the broader modifier-decoding gap
on varchar(n)/numeric(p,s) — tracked separately.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: End-to-end live-Postgres integration test

**Files:**
- Create: `tests/integration/test_pgvector_live.py`

- [ ] **Step 4.1: Write the integration test**

Create `tests/integration/test_pgvector_live.py` with:

```python
"""Live-Postgres integration test for pgvector foundation.

Verifies the end-to-end path with the `vector` extension installed:
  (a) `introspect_schema` recovers the dimension from atttypmod.
  (b) The generator emits valid vector literals.
  (c) Generated rows round-trip through psycopg into Postgres.

Skips if:
  - SQLPROOF_TEST_DATABASE_URL is unset, OR
  - CREATE EXTENSION vector fails on the target server (pgvector not
    installed).
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest

from sqlproof.schema.introspect import introspect_schema
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA_SQL = """
CREATE TABLE embeddings (
    id serial PRIMARY KEY,
    embedding vector(8) NOT NULL
);
"""


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_vector_column_round_trips_through_postgres() -> None:
    from sqlproof import SqlProof
    from sqlproof.config import SqlProofConfig

    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    schema_name = f"sqlproof_pgvector_{uuid4().hex}"

    with psycopg.connect(dsn, autocommit=True) as connection:
        # Probe for pgvector availability. CREATE EXTENSION at the
        # database level requires superuser; skip cleanly when missing
        # rather than failing the suite on hosts without pgvector.
        try:
            connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except psycopg.Error as exc:
            pytest.skip(f"pgvector extension not available: {exc}")

        connection.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            connection.execute(f'SET search_path TO "{schema_name}"')
            for statement in SCHEMA_SQL.strip().split(";"):
                if statement.strip():
                    connection.execute(statement)

            # Parser-side
            parsed = parse_schema_sql(SCHEMA_SQL).table("embeddings")
            assert parsed.column("embedding").type.name == "vector"
            assert parsed.column("embedding").type.modifiers == (8,)

            # Introspector-side
            with connection.cursor(row_factory=psycopg.rows.dict_row) as cur:
                introspected = introspect_schema(cur, schema=schema_name).table(
                    "embeddings", schema=schema_name
                )
            embedding_type = introspected.column("embedding").type
            assert embedding_type.name == "vector"
            assert embedding_type.modifiers == (8,)

            # End-to-end: generate and insert via SqlProof.check.
            # If the wire format breaks anywhere in the chain, this
            # raises before the property body runs.
            proof = SqlProof.from_config(
                SqlProofConfig(connection_string=dsn, schema=schema_name)
            )

            def property_check(db) -> None:
                rows = db.query(
                    f'SELECT embedding FROM "{schema_name}".embeddings'
                )
                assert all(row["embedding"] is not None for row in rows)

            proof.check(
                "vector-typed columns round-trip cleanly",
                sizes={"embeddings": 4},
                property=property_check,
                runs=2,
            )
        finally:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
```

- [ ] **Step 4.2: Run the test with a live database**

Pre-req: a Postgres with pgvector available. Inbox sample's Supabase image works; any postgres image with pgvector installed works.

```bash
SQLPROOF_TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:54322/postgres' \
  uv run pytest tests/integration/test_pgvector_live.py -v
```

Expected: PASS (or SKIPPED if pgvector isn't available on the test database — in which case verify on a host that has it).

- [ ] **Step 4.3: Run without a DSN to confirm the skip works**

```bash
uv run pytest tests/integration/test_pgvector_live.py -v
```

Expected: SKIPPED with reason "set SQLPROOF_TEST_DATABASE_URL...".

- [ ] **Step 4.4: Lint**

Run: `uv run ruff check tests/integration/test_pgvector_live.py`

Expected: clean.

- [ ] **Step 4.5: Commit**

```bash
git add tests/integration/test_pgvector_live.py
git commit -m "$(cat <<'EOF'
test(integration): round-trip vector(N) column through live Postgres

Covers parse → introspect → generate → INSERT for a pgvector column.
Skips cleanly when SQLPROOF_TEST_DATABASE_URL is unset or when the
target server lacks the pgvector extension.

Refs #69

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Inbox sample cleanup (rebase-gated)

**Pre-condition:** `feat/inbox-sample` must be merged to `main` (or this branch rebased onto it). If `examples/inbox/tests/_helpers.py` does not exist on the current branch, skip this task and revisit after the rebase.

**Files:**
- Delete: `examples/inbox/tests/_helpers.py`
- Modify: `examples/inbox/tests/test_similar_tickets.py`
- Modify: `examples/inbox/tests/test_hybrid_search.py`

- [ ] **Step 5.1: Confirm the rebase has happened**

Run: `ls examples/inbox/tests/_helpers.py`

Expected: file exists. If missing, the rebase hasn't happened yet — stop and rebase first:

```bash
git fetch origin
git rebase origin/main   # after feat/inbox-sample merges
# or, before that merge: git rebase feat/inbox-sample
```

- [ ] **Step 5.2: Run the inbox suite to capture baseline failure counts**

```bash
SUPABASE_DB_URL='...' uv run pytest examples/inbox/tests -v
```

Record which tests fail and why. The cleanup should not change *which* tests fail (each recipe's intentional bug should still surface).

- [ ] **Step 5.3: Strip the override from `test_similar_tickets.py`**

In `examples/inbox/tests/test_similar_tickets.py`:

1. Remove `from _helpers import vector_strategy`.
2. Remove the `"message_embeddings.embedding": vector_strategy(384)` entry from the column-overrides dict.
3. Remove the docstring sentence referencing issue #69 and the vector_strategy workaround.

- [ ] **Step 5.4: Strip the override from `test_hybrid_search.py`**

Same three sub-steps as Step 5.3, applied to `examples/inbox/tests/test_hybrid_search.py`.

- [ ] **Step 5.5: Delete `_helpers.py`**

```bash
git rm examples/inbox/tests/_helpers.py
```

- [ ] **Step 5.6: Re-run the inbox suite**

```bash
SUPABASE_DB_URL='...' uv run pytest examples/inbox/tests -v
```

Expected: every test that was failing before still fails (same recipe bug surfaces), every test that was passing before still passes. The dataset shape changes (generator-supplied vectors instead of user-supplied) but the property-failure pattern must be identical. If a recipe test that previously surfaced its bug stops doing so, **stop and investigate** — the change has masked a regression.

- [ ] **Step 5.7: Lint**

Run: `uv run ruff check examples/inbox/tests/`

Expected: clean.

- [ ] **Step 5.8: Commit**

```bash
git add examples/inbox/tests/test_similar_tickets.py examples/inbox/tests/test_hybrid_search.py
git commit -m "$(cat <<'EOF'
chore(examples): drop vector_strategy workaround in inbox sample

The pgvector foundation now handles vector(N) natively, so the
column-level override is redundant. Removes the helper module and the
two override sites; recipe bugs continue to surface identically.

Refs #69

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Final verification and PR

- [ ] **Step 6.1: Full unit + meta suite**

Run: `uv run pytest tests/unit tests/meta -q`

Expected: all PASS.

- [ ] **Step 6.2: Full integration suite (with DSN)**

Run: `SQLPROOF_TEST_DATABASE_URL='...' uv run pytest tests/integration -q`

Expected: all PASS (or SKIPPED on hosts without pgvector for the new test).

- [ ] **Step 6.3: Full lint and type check**

Run: `uv run ruff check src tests examples && uv run pyright src/sqlproof && uv run mypy src/sqlproof`

Expected: clean.

- [ ] **Step 6.4: Open follow-up tracking issues** *(do once, manually)*

   1. "DSN introspection: decode atttypmod for varchar(n) and numeric(p,s)" — files the broader gap the spec called out.
   2. "contrib/pgvector: distance-aware strategies and invariant helpers" — files the follow-up spec the brainstorm queued.

- [ ] **Step 6.5: Open the PR**

```bash
git push -u origin feat/pgvector-foundation
gh pr create --title "feat: pgvector foundation (closes #69)" --body "$(cat <<'EOF'
## Summary
- Add `vector` branch to `strategy_for_type` so pgvector schemas generate valid datasets without per-column overrides.
- Decode `atttypmod` for vector columns in introspection so DSN-backed schemas behave the same as file-parsed schemas.
- Drop the `vector_strategy(dim)` workaround from the inbox sample.

Spec: `docs/superpowers/specs/2026-06-07-pgvector-foundation-design.md`.

Follow-ups filed separately:
- broader DSN modifier-decoding gap (varchar/numeric)
- `contrib/pgvector` distance-aware strategies and invariant helpers

## Test plan
- [ ] `uv run pytest tests/unit` — green
- [ ] `SQLPROOF_TEST_DATABASE_URL=... uv run pytest tests/integration` — green (new `test_pgvector_live.py` passes against a host with `pgvector`)
- [ ] Inbox suite still surfaces every recipe bug after the helper is removed

Closes #69
EOF
)"
```

---

## Self-review notes

**Spec coverage:**
- Generator branch — Task 1.
- Introspect change — Task 3.
- Parser pinning — Task 2.
- Round-trip integration — Task 4.
- Inbox cleanup — Task 5.
- Follow-up tracking issues — Step 6.4.

**Pinning the contract:**
- Task 1 fixes both the happy path and the missing-dimension error path.
- Task 2 pins parser output so a future refactor can't silently drop the modifier and break Task 1.
- Task 4 covers the introspection SQL change end-to-end (no unit test for the SQL because the change is verifiable only against a live `pg_attribute` row with pgvector loaded).

**Risk callouts already inside tasks:**
- The integration test in Task 4 skips on hosts without pgvector — verify it actually runs on a host with the extension, not just "skips cleanly."
- Task 5 has an explicit "stop and investigate" gate if a recipe bug stops surfacing after the workaround is removed.
- Task 5 cannot start until `feat/inbox-sample` has merged.
