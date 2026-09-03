# Bulk Generation Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give SqlProof a second, linear-time data generation path that produces large, valid, constraint-respecting datasets and loads them into Postgres via `COPY`, without diverging from the existing Hypothesis path.

**Architecture:** Type knowledge moves out of `strategy_for_type`'s branch chain into a declarative `TypeSpec` registry. Both generators become thin interpreters of that registry — Hypothesis strategies for search, seeded RNG samplers for volume — so divergence in type dispatch becomes structurally impossible. The bulk path assigns primary keys deterministically via the existing `_unique_value`, which lets foreign keys be satisfied by arithmetic instead of by sampling materialised parent rows. That is what makes it O(n) and streamable.

**Tech Stack:** Python 3.11+, Hypothesis 6.x, psycopg 3.1+ (`cursor.copy()`), pytest, pglast (existing), Postgres 15 via the `sqlproof-pg` container.

**Spec:** `docs/superpowers/specs/2026-09-03-function-scale-analysis-design.md` (Phase 1 only — the probe, sweep, fit, artifact and CLI are Phase 2 and are out of scope here.)

## Global Constraints

- Python 3.11+ (`pyproject.toml` classifiers: 3.11, 3.12). Use `from __future__ import annotations` in every new module, matching existing files.
- Coverage gate: `uv run pytest --cov=sqlproof --cov-fail-under=95`. New modules must be covered.
- Integration tests live in `tests/integration/`, are gated on the `SQLPROOF_TEST_DATABASE_URL` env var, and skip when it is unset. Copy the `pytestmark = pytest.mark.skipif(...)` pattern from `tests/integration/test_fk_cycle_resolution.py:33-37`.
- Local Postgres for integration work, per `CONTRIBUTING.md:36-44`:
  ```bash
  docker run -d --name sqlproof-pg -e POSTGRES_PASSWORD=postgres \
    -p 54399:5432 supabase/postgres:15.8.1.040
  export SQLPROOF_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54399/postgres
  ```
- Public API of `columns.py` must not break: `strategy_for_column` and `strategy_for_type` keep their current signatures and behaviour. Existing tests are the safety net for the refactor.
- Text values must never contain `\x00` (Postgres rejects it) and must exclude Unicode surrogate category `Cs`. This is already encoded in `POSTGRES_TEXT_ALPHABET` (`columns.py:15-18`); the bulk path must honour the same restriction.
- Never assert the two generation paths produce the *same values*. The contract is validity, type coverage, and statistical shape only — see the spec's "The main risk" section.

## File Structure

| File | Responsibility |
|---|---|
| `src/sqlproof/generators/typespec.py` (new) | `TypeSpec` dataclass, the name→spec registry, `spec_for_type`. Pure data; no Hypothesis, no RNG, no DB. |
| `src/sqlproof/generators/columns.py` (modify) | Becomes the Hypothesis *interpreter* of `TypeSpec`. Keeps its public functions as wrappers. |
| `src/sqlproof/generators/bulk.py` (new) | The bulk *interpreter*: seeded samplers, plus streaming row generation with deterministic keys and skew. |
| `src/sqlproof/generators/rows.py` (modify) | Hoist loop-invariant strategy construction out of the per-row loop. |
| `src/sqlproof/testing.py` (modify) | Build out `schemas()` into a real schema strategy. |
| `src/sqlproof/scale/__init__.py` (new) | Package marker. |
| `src/sqlproof/scale/load.py` (new) | `COPY` streaming into Postgres and `ANALYZE`. The only new module that touches a database. |

Tests mirror the layout: `tests/unit/test_typespec.py`, `tests/unit/test_bulk_generator.py`, `tests/unit/test_schemas_strategy.py`, `tests/integration/test_bulk_copy_live.py`, `tests/integration/test_cross_path_consistency_live.py`, `tests/integration/test_pg_stats_parity_live.py`.

---

### Task 1: Hoist loop-invariant strategy construction in `rows.py`

Independent of everything else and shippable on its own. `rows.py` rebuilds objects on every row that depend only on the column or the table.

**Files:**
- Modify: `src/sqlproof/generators/rows.py:108`, `src/sqlproof/generators/rows.py:123`
- Test: `tests/unit/test_rows_hoisting.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: no API change. `table_rows_strategy` keeps its exact signature.

- [ ] **Step 1: Write the failing test**

The observable property is *how many times* the strategy is constructed, not timing (timing tests are flaky). Count calls with a spy.

```python
# tests/unit/test_rows_hoisting.py
from __future__ import annotations

from hypothesis import given, settings, HealthCheck

from sqlproof.generators import rows as rows_module
from sqlproof.generators.rows import table_rows_strategy
from sqlproof.schema.parse_sql import parse_schema_sql


def test_column_strategy_built_once_per_column_not_once_per_row(monkeypatch):
    schema = parse_schema_sql(
        "CREATE TABLE t (id bigint PRIMARY KEY, a text NOT NULL, b text NOT NULL);"
    )
    table = schema.table("t")
    calls = {"n": 0}
    original = rows_module.strategy_for_column

    def counting(column):
        calls["n"] += 1
        return original(column)

    monkeypatch.setattr(rows_module, "strategy_for_column", counting)

    @given(table_rows_strategy(table, count=50))
    @settings(max_examples=1, deadline=None, database=None,
              suppress_health_check=list(HealthCheck))
    def run(generated):
        assert len(generated) == 50

    run()
    # 2 non-PK columns. Without hoisting this is 100 (50 rows x 2 columns).
    assert calls["n"] <= 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_rows_hoisting.py -v`
Expected: FAIL — `assert 100 <= 2`.

- [ ] **Step 3: Hoist the per-column strategy**

In `rows.py`, before the `for index in range(count):` loop inside the `rows` composite (currently around line 80), build the refined strategy once per column. Add above the loop:

```python
        # Loop-invariant: the refined strategy depends only on the
        # column and the table's checks, never on the row index.
        # Building it per row cost ~2x at n=40,000 (see the scale
        # analysis design doc).
        refined_by_column: dict[str, SearchStrategy[Any]] = {}
        for column in table.columns:
            if column.is_generated or column.default is not None:
                continue
            refined_by_column[column.name] = refine_for_checks(
                column,
                strategy_for_column(column),
                table.check_constraints + _domain_checks_as_column_checks(column),
            )
```

Then replace the in-loop construction at `rows.py:123-127`:

```python
                strategy = refined_by_column[column.name]
                row[column.name] = draw(strategy)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_rows_hoisting.py -v`
Expected: PASS

- [ ] **Step 5: Hoist the FK parent strategy**

`rows.py:108` constructs `st.sampled_from(parents)` per row over the whole parent list. Build one per FK column before the loop. Add alongside `refined_by_column`:

```python
        # Loop-invariant: one sampled_from per FK column, not per row.
        # Constructing it per row is O(len(parents)) each time.
        parent_strategy_by_column: dict[str, SearchStrategy[dict[str, Any]]] = {}
        for column in table.columns:
            fk = _foreign_key_for_column(table, column.name)
            if fk is None:
                continue
            parent_key = _parent_rows_key(fk, parent_rows)
            if parent_key is None:
                continue
            available = parent_rows[parent_key]
            if available:
                parent_strategy_by_column[column.name] = st.sampled_from(available)
```

Replace the body at `rows.py:104-112` so the FK branch reads:

```python
                fk = _foreign_key_for_column(table, column.name)
                if fk is not None:
                    parent_strategy = parent_strategy_by_column.get(column.name)
                    if parent_strategy is not None:
                        parent = draw(parent_strategy)
                        row[column.name] = parent[fk.referenced_columns[0]]
                        continue
                    if column.nullable:
                        row[column.name] = None
                        continue
                    msg = (
                        f"Cannot generate {table.name}.{column.name}: "
                        f"required foreign key has no available parent rows for "
                        f"{fk.referenced_table}.{fk.referenced_columns[0]}."
                    )
                    raise SqlProofGenerationError(msg)
```

- [ ] **Step 6: Run the full suite to prove no behaviour change**

Run: `uv run pytest tests/unit tests/meta -q`
Expected: PASS. This refactor must not change generated data, only how often strategies are built.

- [ ] **Step 7: Commit**

```bash
git add src/sqlproof/generators/rows.py tests/unit/test_rows_hoisting.py
git commit -m "perf(generators): hoist loop-invariant strategy construction

rows.py rebuilt the refined column strategy per row per column and a
fresh sampled_from over the whole parent list per row. Both depend only
on the column and table. Measured ~2x cost at n=40,000 from the FK case
alone."
```

---

### Task 2: The `TypeSpec` registry

**Files:**
- Create: `src/sqlproof/generators/typespec.py`
- Test: `tests/unit/test_typespec.py` (create)

**Interfaces:**
- Consumes: `sqlproof.schema.model.PgType`, `Column`.
- Produces:
  - `TypeSpec` — frozen dataclass, fields as written below.
  - `spec_for_type(pg_type: PgType) -> TypeSpec`
  - `spec_for_column(column: Column) -> tuple[TypeSpec, bool]` returning `(spec, nullable)`
  - `TYPE_SPEC_BUILDERS: dict[str, Callable[[PgType], TypeSpec]]`
  - `KNOWN_TYPE_NAMES: frozenset[str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_typespec.py
from __future__ import annotations

import pytest

from sqlproof.generators.typespec import (
    KNOWN_TYPE_NAMES,
    TYPE_SPEC_BUILDERS,
    spec_for_column,
    spec_for_type,
)
from sqlproof.schema.model import Column, PgType


def test_bigint_spec_carries_int8_bounds():
    spec = spec_for_type(PgType("scalar", "bigint"))
    assert spec.kind == "integer"
    assert spec.min_value == -(2**63)
    assert spec.max_value == 2**63 - 1


def test_varchar_spec_honours_modifier():
    spec = spec_for_type(PgType("scalar", "varchar", modifiers=(40,)))
    assert spec.kind == "text"
    assert spec.max_size == 40


def test_char_spec_is_fixed_width():
    spec = spec_for_type(PgType("scalar", "char", modifiers=(8,)))
    assert spec.min_size == 8
    assert spec.max_size == 8


def test_enum_spec_carries_values():
    spec = spec_for_type(PgType("enum", "mood", enum_values=("sad", "ok")))
    assert spec.kind == "enum"
    assert spec.enum_values == ("sad", "ok")


def test_domain_unwraps_to_base():
    spec = spec_for_type(
        PgType("domain", "positive_int", base=PgType("scalar", "integer"))
    )
    assert spec.kind == "integer"


def test_range_carries_element_spec():
    spec = spec_for_type(PgType("range", "int4range", base=PgType("scalar", "integer")))
    assert spec.kind == "range"
    assert spec.element is not None
    assert spec.element.kind == "integer"


def test_tstzrange_element_is_tz_aware():
    spec = spec_for_type(
        PgType("range", "tstzrange", base=PgType("scalar", "timestamptz"))
    )
    assert spec.element is not None
    assert spec.element.tz_aware is True


def test_vector_requires_dimension():
    from sqlproof.exceptions import SqlProofSchemaError

    with pytest.raises(SqlProofSchemaError):
        spec_for_type(PgType("scalar", "vector"))


def test_unknown_type_falls_back_to_text():
    spec = spec_for_type(PgType("scalar", "some_extension_type"))
    assert spec.kind == "text"


def test_nullable_flag_travels_with_column():
    col = Column("a", PgType("scalar", "integer"), True, None, False)
    spec, nullable = spec_for_column(col)
    assert spec.kind == "integer"
    assert nullable is True


def test_every_registered_name_builds_a_spec():
    for name in KNOWN_TYPE_NAMES:
        builder = TYPE_SPEC_BUILDERS[name]
        modifiers = (8, 2) if name in {"numeric", "decimal"} else (8,)
        assert builder(PgType("scalar", name, modifiers=modifiers)) is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_typespec.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqlproof.generators.typespec'`

- [ ] **Step 3: Write the registry**

```python
# src/sqlproof/generators/typespec.py
"""Declarative type knowledge, consumed by both generation paths.

This module holds what is *true* about a Postgres type — bounds,
sizes, precision, element types. It contains no Hypothesis strategies
and no RNG. `columns.py` interprets a TypeSpec into a Hypothesis
strategy; `bulk.py` interprets the same TypeSpec into a seeded
sampler.

Adding support for a new type means adding one entry here. Because
neither generator holds type knowledge of its own, it is not possible
to teach one path about a type and forget the other.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlproof.exceptions import SqlProofSchemaError
from sqlproof.schema.model import Column, PgType

SpecKind = Literal[
    "integer", "decimal", "float", "boolean", "text", "uuid",
    "datetime", "date", "time", "interval", "json", "binary",
    "vector", "enum", "range", "composite",
]


@dataclass(frozen=True, slots=True)
class TypeSpec:
    kind: SpecKind
    min_value: int | None = None
    max_value: int | None = None
    places: int | None = None
    min_size: int | None = None
    max_size: int | None = None
    float_width: int | None = None
    enum_values: tuple[str, ...] = ()
    dimension: int | None = None
    element: TypeSpec | None = None
    fields: tuple[tuple[str, TypeSpec], ...] = ()
    tz_aware: bool = False


def _int(lo: int, hi: int) -> Callable[[PgType], TypeSpec]:
    return lambda _t: TypeSpec(kind="integer", min_value=lo, max_value=hi)


def _numeric(t: PgType) -> TypeSpec:
    places = t.modifiers[1] if len(t.modifiers) > 1 else 2
    return TypeSpec(kind="decimal", min_value=-1_000_000, max_value=1_000_000, places=places)


def _varchar(t: PgType) -> TypeSpec:
    return TypeSpec(kind="text", max_size=t.modifiers[0] if t.modifiers else 255)


def _char(t: PgType) -> TypeSpec:
    size = t.modifiers[0] if t.modifiers else 1
    return TypeSpec(kind="text", min_size=size, max_size=size)


def _vector(t: PgType) -> TypeSpec:
    if not t.modifiers:
        msg = (
            "vector type requires a dimension (e.g. vector(384)); "
            "got vector with no modifier"
        )
        raise SqlProofSchemaError(msg)
    return TypeSpec(kind="vector", dimension=t.modifiers[0])


def _timestamp(t: PgType) -> TypeSpec:
    # Plain timestamptz columns use naive datetimes, matching the
    # existing behaviour in columns.py. Only tstzrange elements need
    # tz-aware values for psycopg's adapter to type them correctly.
    return TypeSpec(kind="datetime", tz_aware=False)


TYPE_SPEC_BUILDERS: dict[str, Callable[[PgType], TypeSpec]] = {
    "smallint": _int(-32_768, 32_767),
    "int2": _int(-32_768, 32_767),
    "integer": _int(-2_147_483_648, 2_147_483_647),
    "int": _int(-2_147_483_648, 2_147_483_647),
    "int4": _int(-2_147_483_648, 2_147_483_647),
    "serial": _int(-2_147_483_648, 2_147_483_647),
    "bigint": _int(-(2**63), 2**63 - 1),
    "int8": _int(-(2**63), 2**63 - 1),
    "bigserial": _int(-(2**63), 2**63 - 1),
    "numeric": _numeric,
    "decimal": _numeric,
    "real": lambda _t: TypeSpec(kind="float", float_width=32),
    "float4": lambda _t: TypeSpec(kind="float", float_width=32),
    "double precision": lambda _t: TypeSpec(kind="float"),
    "float8": lambda _t: TypeSpec(kind="float"),
    "boolean": lambda _t: TypeSpec(kind="boolean"),
    "bool": lambda _t: TypeSpec(kind="boolean"),
    "text": lambda _t: TypeSpec(kind="text", max_size=255),
    "citext": lambda _t: TypeSpec(kind="text", max_size=255),
    "varchar": _varchar,
    "character varying": _varchar,
    "char": _char,
    "character": _char,
    "uuid": lambda _t: TypeSpec(kind="uuid"),
    "timestamp": _timestamp,
    "timestamp without time zone": _timestamp,
    "timestamptz": _timestamp,
    "timestamp with time zone": _timestamp,
    "date": lambda _t: TypeSpec(kind="date"),
    "time": lambda _t: TypeSpec(kind="time"),
    "timetz": lambda _t: TypeSpec(kind="time"),
    "interval": lambda _t: TypeSpec(kind="interval"),
    "json": lambda _t: TypeSpec(kind="json"),
    "jsonb": lambda _t: TypeSpec(kind="json"),
    "bytea": lambda _t: TypeSpec(kind="binary"),
    "vector": _vector,
}

KNOWN_TYPE_NAMES: frozenset[str] = frozenset(TYPE_SPEC_BUILDERS)

_FALLBACK = TypeSpec(kind="text", max_size=255)


def spec_for_type(pg_type: PgType) -> TypeSpec:
    if pg_type.kind == "enum":
        return TypeSpec(kind="enum", enum_values=pg_type.enum_values)
    if pg_type.kind == "domain" and pg_type.base is not None:
        # A domain is an alias plus optional CHECKs. The CHECKs are
        # enforced downstream in rows.py's refinement pipeline, which
        # knows the column name to substitute for VALUE.
        return spec_for_type(pg_type.base)
    if pg_type.kind == "range" and pg_type.base is not None:
        element = spec_for_type(pg_type.base)
        if pg_type.name == "tstzrange":
            element = TypeSpec(kind="datetime", tz_aware=True)
        return TypeSpec(kind="range", element=element)
    if pg_type.kind == "composite":
        return TypeSpec(
            kind="composite",
            fields=tuple((n, spec_for_type(t)) for n, t in pg_type.composite_fields),
        )
    builder = TYPE_SPEC_BUILDERS.get(pg_type.name.lower())
    if builder is None:
        return _FALLBACK
    return builder(pg_type)


def spec_for_column(column: Column) -> tuple[TypeSpec, bool]:
    return spec_for_type(column.type), column.nullable
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_typespec.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/generators/typespec.py tests/unit/test_typespec.py
git commit -m "feat(generators): add declarative TypeSpec registry

Single source of truth for Postgres type knowledge. Both generation
paths will become interpreters of this, so a type cannot be taught to
one path and forgotten by the other."
```

---

### Task 3: Make `columns.py` an interpreter of `TypeSpec`

Behaviour-preserving refactor. The existing test suite is the safety net.

**Files:**
- Modify: `src/sqlproof/generators/columns.py`
- Test: existing `tests/unit/` and `tests/meta/` suites

**Interfaces:**
- Consumes: `TypeSpec`, `spec_for_type`, `spec_for_column` from Task 2.
- Produces: `strategy_for_spec(spec: TypeSpec) -> SearchStrategy[Any]`. `strategy_for_type(pg_type)` and `strategy_for_column(column)` keep their existing signatures and become wrappers.

- [ ] **Step 1: Capture current behaviour as a characterisation test**

```python
# tests/unit/test_columns_interpreter.py
from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings

from sqlproof.generators.columns import strategy_for_type
from sqlproof.schema.model import PgType


@given(strategy_for_type(PgType("scalar", "smallint")))
@settings(max_examples=25, deadline=None)
def test_smallint_stays_in_range(v):
    assert -32_768 <= v <= 32_767


@given(strategy_for_type(PgType("scalar", "varchar", modifiers=(12,))))
@settings(max_examples=25, deadline=None)
def test_varchar_respects_modifier(v):
    assert len(v) <= 12
    assert "\x00" not in v


@given(strategy_for_type(PgType("scalar", "numeric", modifiers=(10, 3))))
@settings(max_examples=25, deadline=None)
def test_numeric_respects_scale(v):
    assert isinstance(v, Decimal)
    assert -Decimal("1000000") <= v <= Decimal("1000000")
```

- [ ] **Step 2: Run to verify it passes against current code**

Run: `uv run pytest tests/unit/test_columns_interpreter.py -v`
Expected: PASS — this pins current behaviour *before* the refactor.

- [ ] **Step 3: Rewrite `strategy_for_type` as a thin wrapper over the registry**

Replace the body of `strategy_for_type` and add `strategy_for_spec`. Keep `POSTGRES_TEXT_ALPHABET`, `_postgres_text` and the module's public names unchanged.

```python
from sqlproof.generators.typespec import TypeSpec, spec_for_type as _spec_for_type


def strategy_for_type(pg_type: PgType) -> SearchStrategy[Any]:
    return strategy_for_spec(_spec_for_type(pg_type))


def strategy_for_spec(spec: TypeSpec) -> SearchStrategy[Any]:
    if spec.kind == "integer":
        assert spec.min_value is not None and spec.max_value is not None
        return st.integers(spec.min_value, spec.max_value)
    if spec.kind == "decimal":
        return st.decimals(
            min_value=Decimal(spec.min_value or 0),
            max_value=Decimal(spec.max_value or 0),
            places=spec.places,
            allow_nan=False,
            allow_infinity=False,
        )
    if spec.kind == "float":
        if spec.float_width == 32:
            return st.floats(width=32, allow_nan=False, allow_infinity=False)
        return st.floats(allow_nan=False, allow_infinity=False)
    if spec.kind == "boolean":
        return st.booleans()
    if spec.kind == "text":
        return _postgres_text(min_size=spec.min_size or 0, max_size=spec.max_size)
    if spec.kind == "uuid":
        return st.uuids().map(str)
    if spec.kind == "datetime":
        if spec.tz_aware:
            from datetime import UTC

            return st.datetimes(timezones=st.just(UTC))
        return st.datetimes()
    if spec.kind == "date":
        return st.dates()
    if spec.kind == "time":
        return st.times()
    if spec.kind == "interval":
        return st.timedeltas()
    if spec.kind == "json":
        json_scalar = (
            st.none()
            | st.booleans()
            | st.floats(allow_nan=False, allow_infinity=False)
            | _postgres_text()
        )
        return st.recursive(
            json_scalar,
            lambda children: (
                st.lists(children, max_size=5)
                | st.dictionaries(_postgres_text(max_size=20), children, max_size=5)
            ),
            max_leaves=10,
        )
    if spec.kind == "binary":
        return st.binary()
    if spec.kind == "vector":
        assert spec.dimension is not None
        # Bounded integers rather than st.floats: bounded-float draws
        # spend ~9 bytes of Hypothesis entropy each, exhausting the 8KB
        # conjecture buffer at common embedding dimensions (1536, 2000).
        # Integers spend ~3. Shrink target is identical.
        component = st.integers(min_value=-1_000_000, max_value=1_000_000)
        return st.lists(component, min_size=spec.dimension, max_size=spec.dimension).map(
            lambda xs: "[" + ",".join(f"{x / 1_000_000:.6f}" for x in xs) + "]"
        )
    if spec.kind == "enum":
        return st.sampled_from(spec.enum_values)
    if spec.kind == "range":
        assert spec.element is not None
        element_strategy = strategy_for_spec(spec.element)
        return (
            st.tuples(element_strategy, element_strategy)
            .filter(lambda pair: pair[0] != pair[1])
            .map(lambda pair: Range(min(pair), max(pair), "[)"))
        )
    if spec.kind == "composite":
        return st.fixed_dictionaries(
            {name: strategy_for_spec(sub) for name, sub in spec.fields}
        )
    return _postgres_text(max_size=255)
```

Delete the now-dead `_range_strategy` helper and the old branch chain.

- [ ] **Step 4: Run the characterisation test and the full suite**

Run: `uv run pytest tests/unit tests/meta -q`
Expected: PASS. Any failure means the refactor changed behaviour — fix the interpreter, not the test.

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/generators/columns.py tests/unit/test_columns_interpreter.py
git commit -m "refactor(generators): make columns.py a TypeSpec interpreter

strategy_for_type now resolves a TypeSpec and interprets it. Type
knowledge lives only in typespec.py. Public API unchanged."
```

---

### Task 4: The bulk sampler interpreter

**Files:**
- Create: `src/sqlproof/generators/bulk.py`
- Test: `tests/unit/test_bulk_generator.py` (create)

**Interfaces:**
- Consumes: `TypeSpec`, `spec_for_column` (Task 2).
- Produces:
  - `sampler_for_spec(spec: TypeSpec, rng: random.Random) -> Callable[[], Any]`
  - `sampler_for_column(column: Column, rng: random.Random, *, null_frac: float = DEFAULT_NULL_FRAC) -> Callable[[], Any]`
  - `DEFAULT_NULL_FRAC: float`
  - `BULK_TEXT_ALPHABET: str`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_bulk_generator.py
from __future__ import annotations

import random
from decimal import Decimal

from sqlproof.generators.bulk import sampler_for_column, sampler_for_spec
from sqlproof.generators.typespec import TypeSpec, spec_for_type
from sqlproof.schema.model import Column, PgType


def test_integer_sampler_respects_bounds():
    spec = spec_for_type(PgType("scalar", "smallint"))
    sample = sampler_for_spec(spec, random.Random(1))
    for _ in range(200):
        assert -32_768 <= sample() <= 32_767


def test_text_sampler_respects_max_size_and_excludes_nul():
    spec = spec_for_type(PgType("scalar", "varchar", modifiers=(10,)))
    sample = sampler_for_spec(spec, random.Random(1))
    for _ in range(200):
        v = sample()
        assert len(v) <= 10
        assert "\x00" not in v


def test_char_sampler_is_exactly_fixed_width():
    spec = spec_for_type(PgType("scalar", "char", modifiers=(6,)))
    sample = sampler_for_spec(spec, random.Random(1))
    assert all(len(sample()) == 6 for _ in range(50))


def test_numeric_sampler_respects_scale():
    spec = spec_for_type(PgType("scalar", "numeric", modifiers=(10, 3)))
    sample = sampler_for_spec(spec, random.Random(1))
    v = sample()
    assert isinstance(v, Decimal)
    assert -v.as_tuple().exponent == 3


def test_enum_sampler_only_emits_declared_values():
    spec = spec_for_type(PgType("enum", "mood", enum_values=("sad", "ok", "great")))
    sample = sampler_for_spec(spec, random.Random(1))
    assert {sample() for _ in range(100)} <= {"sad", "ok", "great"}


def test_same_seed_produces_identical_sequences():
    spec = spec_for_type(PgType("scalar", "bigint"))
    a = [sampler_for_spec(spec, random.Random(7))() for _ in range(5)]
    b = [sampler_for_spec(spec, random.Random(7))() for _ in range(5)]
    assert a == b


def test_nullable_column_produces_nulls_at_roughly_the_configured_rate():
    col = Column("a", PgType("scalar", "integer"), True, None, False)
    sample = sampler_for_column(col, random.Random(3), null_frac=0.25)
    values = [sample() for _ in range(4000)]
    observed = values.count(None) / len(values)
    assert 0.20 < observed < 0.30


def test_non_nullable_column_never_produces_null():
    col = Column("a", PgType("scalar", "integer"), False, None, False)
    sample = sampler_for_column(col, random.Random(3), null_frac=0.9)
    assert all(sample() is not None for _ in range(500))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bulk_generator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqlproof.generators.bulk'`

- [ ] **Step 3: Write the sampler interpreter**

```python
# src/sqlproof/generators/bulk.py
"""Bulk generation path: seeded samplers over TypeSpec.

The Hypothesis path exists to *search* — every draw is recorded so it
can be shrunk and replayed, which costs more per draw the more has
already been drawn in one example. This path exists to produce
*volume*, so it records nothing and is O(1) per value.

It is an interpreter of the same TypeSpec registry that columns.py
interprets. It holds no type knowledge of its own.
"""

from __future__ import annotations

import random
import string
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlproof.generators.typespec import TypeSpec, spec_for_column
from sqlproof.schema.model import Column

# Excludes \x00 (Postgres rejects it in text) and stays inside ASCII,
# which keeps COPY output compact. The Hypothesis path deliberately
# explores a much wider alphabet -- that is a search concern, not a
# volume concern, and the paths are not required to agree on values.
BULK_TEXT_ALPHABET = string.ascii_letters + string.digits + " _-."

DEFAULT_NULL_FRAC = 0.1

_EPOCH_DATE = date(2000, 1, 1)


def sampler_for_spec(spec: TypeSpec, rng: random.Random) -> Callable[[], Any]:
    kind = spec.kind
    if kind == "integer":
        lo, hi = spec.min_value or 0, spec.max_value or 0
        return lambda: rng.randint(lo, hi)
    if kind == "decimal":
        lo, hi = spec.min_value or 0, spec.max_value or 0
        places = spec.places if spec.places is not None else 2
        quant = Decimal(1).scaleb(-places)
        return lambda: (
            Decimal(rng.uniform(lo, hi)).quantize(quant)
        )
    if kind == "float":
        return lambda: rng.uniform(-1e6, 1e6)
    if kind == "boolean":
        return lambda: rng.random() < 0.5
    if kind == "text":
        lo = spec.min_size or 0
        hi = spec.max_size if spec.max_size is not None else 255
        hi = max(hi, lo)
        return lambda: "".join(
            rng.choice(BULK_TEXT_ALPHABET) for _ in range(rng.randint(lo, hi))
        )
    if kind == "uuid":
        return lambda: str(UUID(int=rng.getrandbits(128)))
    if kind == "datetime":
        if spec.tz_aware:
            return lambda: datetime(2000, 1, 1, tzinfo=UTC) + timedelta(
                seconds=rng.randint(0, 10**9)
            )
        return lambda: datetime(2000, 1, 1) + timedelta(seconds=rng.randint(0, 10**9))
    if kind == "date":
        return lambda: _EPOCH_DATE + timedelta(days=rng.randint(0, 20_000))
    if kind == "time":
        return lambda: time(rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
    if kind == "interval":
        return lambda: timedelta(seconds=rng.randint(0, 10**7))
    if kind == "json":
        text_sampler = sampler_for_spec(TypeSpec(kind="text", max_size=12), rng)
        return lambda: {text_sampler(): rng.randint(0, 1000)}
    if kind == "binary":
        return lambda: bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 32)))
    if kind == "vector":
        dim = spec.dimension or 0
        return lambda: (
            "[" + ",".join(f"{rng.uniform(-1, 1):.6f}" for _ in range(dim)) + "]"
        )
    if kind == "enum":
        values = spec.enum_values
        return lambda: rng.choice(values)
    if kind == "range":
        assert spec.element is not None
        element = sampler_for_spec(spec.element, rng)

        def draw_range() -> Any:
            from psycopg.types.range import Range

            a, b = element(), element()
            while a == b:
                b = element()
            return Range(min(a, b), max(a, b), "[)")

        return draw_range
    if kind == "composite":
        field_samplers = tuple(
            (name, sampler_for_spec(sub, rng)) for name, sub in spec.fields
        )
        return lambda: {name: fn() for name, fn in field_samplers}
    return sampler_for_spec(TypeSpec(kind="text", max_size=255), rng)


def sampler_for_column(
    column: Column,
    rng: random.Random,
    *,
    null_frac: float = DEFAULT_NULL_FRAC,
) -> Callable[[], Any]:
    spec, nullable = spec_for_column(column)
    base = sampler_for_spec(spec, rng)
    if not nullable:
        return base
    return lambda: None if rng.random() < null_frac else base()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bulk_generator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/generators/bulk.py tests/unit/test_bulk_generator.py
git commit -m "feat(generators): add seeded bulk samplers over TypeSpec"
```

---

### Task 5: Registry exhaustiveness — both interpreters cover every entry

The structural guarantee needs one test to prove it holds.

**Files:**
- Test: `tests/unit/test_interpreter_exhaustiveness.py` (create)

**Interfaces:**
- Consumes: `KNOWN_TYPE_NAMES`, `TYPE_SPEC_BUILDERS`, `spec_for_type` (Task 2); `strategy_for_spec` (Task 3); `sampler_for_spec` (Task 4).
- Produces: nothing.

- [ ] **Step 1: Write the test**

```python
# tests/unit/test_interpreter_exhaustiveness.py
"""Both generation paths must interpret every registered type.

This is the backstop for the structural guarantee in typespec.py: a
type added to the registry but unhandled by one interpreter fails here,
in CI, rather than on a user's schema.
"""
from __future__ import annotations

import random

import pytest

from sqlproof.generators.bulk import sampler_for_spec
from sqlproof.generators.columns import strategy_for_spec
from sqlproof.generators.typespec import (
    KNOWN_TYPE_NAMES,
    TYPE_SPEC_BUILDERS,
    spec_for_type,
)
from sqlproof.schema.model import PgType

# vector legitimately requires a modifier; give every builder one that
# satisfies it. numeric/decimal read modifiers[1] for scale.
def _pg_type(name: str) -> PgType:
    modifiers = (10, 2) if name in {"numeric", "decimal"} else (8,)
    return PgType("scalar", name, modifiers=modifiers)


@pytest.mark.parametrize("name", sorted(KNOWN_TYPE_NAMES))
def test_hypothesis_interpreter_handles_every_registered_type(name):
    spec = TYPE_SPEC_BUILDERS[name](_pg_type(name))
    assert strategy_for_spec(spec) is not None


@pytest.mark.parametrize("name", sorted(KNOWN_TYPE_NAMES))
def test_bulk_interpreter_handles_every_registered_type(name):
    spec = TYPE_SPEC_BUILDERS[name](_pg_type(name))
    sample = sampler_for_spec(spec, random.Random(0))
    sample()  # must not raise


@pytest.mark.parametrize(
    "pg_type",
    [
        PgType("enum", "mood", enum_values=("a", "b")),
        PgType("domain", "d", base=PgType("scalar", "integer")),
        PgType("range", "int4range", base=PgType("scalar", "integer")),
        PgType(
            "composite",
            "addr",
            composite_fields=(("city", PgType("scalar", "text")),),
        ),
    ],
    ids=["enum", "domain", "range", "composite"],
)
def test_both_interpreters_handle_every_type_kind(pg_type):
    spec = spec_for_type(pg_type)
    assert strategy_for_spec(spec) is not None
    sampler_for_spec(spec, random.Random(0))()
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/unit/test_interpreter_exhaustiveness.py -v`
Expected: PASS. If a type fails here, fix the interpreter that is missing it.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_interpreter_exhaustiveness.py
git commit -m "test(generators): assert both interpreters cover the whole registry"
```

---

### Task 6: Streaming bulk row generation with deterministic keys and skew

**Files:**
- Modify: `src/sqlproof/generators/bulk.py`
- Test: `tests/unit/test_bulk_rows.py` (create)

**Interfaces:**
- Consumes: `sampler_for_column` (Task 4); `_unique_value` from `sqlproof.generators.rows` (`rows.py:319`) — the existing deterministic index→key function, reused so both paths assign identical primary keys.
- Produces:
  - `bulk_table_rows(table: Table, *, count: int, rng: random.Random, parent_counts: Mapping[str, int], distribution: str = "uniform", zipf_alpha: float = 1.2, null_frac: float = DEFAULT_NULL_FRAC) -> Iterator[dict[str, Any]]`
  - `parent_index_for(child_index: int, parent_count: int, rng: random.Random, distribution: str, zipf_alpha: float) -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_bulk_rows.py
from __future__ import annotations

import random
from collections import Counter

from sqlproof.generators.bulk import bulk_table_rows, parent_index_for
from sqlproof.generators.rows import _unique_value
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA = """
CREATE TABLE customers (id bigint PRIMARY KEY, email text NOT NULL);
CREATE TABLE orders (
  id bigint PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES customers(id),
  note text
);
"""


def test_primary_keys_match_the_shared_assignment_function():
    schema = parse_schema_sql(SCHEMA)
    rows = list(
        bulk_table_rows(
            schema.table("customers"), count=5,
            rng=random.Random(1), parent_counts={},
        )
    )
    assert [r["id"] for r in rows] == [
        _unique_value("id", "bigint", i) for i in range(5)
    ]


def test_foreign_keys_only_reference_existing_parent_keys():
    schema = parse_schema_sql(SCHEMA)
    valid = {_unique_value("id", "bigint", i) for i in range(10)}
    rows = list(
        bulk_table_rows(
            schema.table("orders"), count=200,
            rng=random.Random(1), parent_counts={"customers": 10},
        )
    )
    assert len(rows) == 200
    assert all(r["customer_id"] in valid for r in rows)


def test_generation_is_streaming_not_materialised():
    schema = parse_schema_sql(SCHEMA)
    stream = bulk_table_rows(
        schema.table("orders"), count=10_000,
        rng=random.Random(1), parent_counts={"customers": 10},
    )
    assert next(iter(stream)) is not None  # yields before generating all 10k


def test_same_seed_reproduces_identical_rows():
    schema = parse_schema_sql(SCHEMA)
    def gen():
        return list(bulk_table_rows(
            schema.table("orders"), count=50,
            rng=random.Random(99), parent_counts={"customers": 5},
        ))
    assert gen() == gen()


def test_uniform_distribution_spreads_children_across_parents():
    counts = Counter(
        parent_index_for(i, 10, random.Random(i), "uniform", 1.2)
        for i in range(2000)
    )
    assert len(counts) == 10
    assert max(counts.values()) < 400  # no parent dominates


def test_zipf_distribution_concentrates_on_few_parents():
    rng = random.Random(5)
    counts = Counter(
        parent_index_for(i, 100, rng, "zipf", 1.2) for i in range(5000)
    )
    top_share = sum(c for _, c in counts.most_common(5)) / 5000
    assert top_share > 0.30  # heavy tenants exist, unlike uniform
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_bulk_rows.py -v`
Expected: FAIL — `ImportError: cannot import name 'bulk_table_rows'`

- [ ] **Step 3: Implement row streaming**

Append to `src/sqlproof/generators/bulk.py`:

```python
from collections.abc import Iterator, Mapping

from sqlproof.generators.rows import (
    _domain_checks_as_column_checks,
    _foreign_key_for_column,
    _is_single_column_unique,
    _unique_value,
)
from sqlproof.schema.model import Table


def parent_index_for(
    child_index: int,
    parent_count: int,
    rng: random.Random,
    distribution: str,
    zipf_alpha: float,
) -> int:
    """Pick a parent row index for a child row.

    Uniform spreads children evenly. Zipf concentrates them on a few
    parents, which is what production data actually looks like and what
    makes a function slow -- one tenant owning a disproportionate share.
    Uniform data systematically under-predicts.
    """
    if parent_count <= 0:
        msg = "parent_index_for requires at least one parent row"
        raise ValueError(msg)
    if distribution == "zipf":
        # Bounded Zipf: resample until inside range. alpha > 1 keeps the
        # expected number of retries small.
        for _ in range(100):
            candidate = rng.zipfvariate(zipf_alpha) if hasattr(rng, "zipfvariate") else None
            if candidate is None:
                candidate = int(rng.paretovariate(zipf_alpha - 1))
            if 1 <= candidate <= parent_count:
                return candidate - 1
        return rng.randrange(parent_count)
    return rng.randrange(parent_count)


def bulk_table_rows(
    table: Table,
    *,
    count: int,
    rng: random.Random,
    parent_counts: Mapping[str, int],
    distribution: str = "uniform",
    zipf_alpha: float = 1.2,
    null_frac: float = DEFAULT_NULL_FRAC,
) -> Iterator[dict[str, Any]]:
    """Yield `count` valid rows for `table`, one at a time.

    Primary keys are *assigned* from the row index via the same
    `_unique_value` the Hypothesis path uses, so both paths produce
    identical keys. That is what lets a foreign key be satisfied by
    arithmetic -- the parent list never has to exist in memory.
    """
    single_pk = table.primary_key[0] if len(table.primary_key) == 1 else None
    samplers: dict[str, Callable[[], Any]] = {}
    for column in table.columns:
        if column.name == single_pk or column.is_generated:
            continue
        if column.default is not None:
            continue
        if _foreign_key_for_column(table, column.name) is not None:
            continue
        if _is_single_column_unique(table, column.name):
            continue
        samplers[column.name] = sampler_for_column(column, rng, null_frac=null_frac)

    for index in range(count):
        row: dict[str, Any] = {}
        for column in table.columns:
            name = column.name
            if column.is_generated or column.default is not None:
                continue
            if name == single_pk:
                row[name] = _unique_value(name, column.type.name, index)
                continue
            fk = _foreign_key_for_column(table, name)
            if fk is not None:
                available = parent_counts.get(fk.referenced_table, 0)
                if available <= 0:
                    if column.nullable:
                        row[name] = None
                        continue
                    msg = (
                        f"Cannot generate {table.name}.{name}: required foreign "
                        f"key has no parent rows for {fk.referenced_table}."
                    )
                    raise SqlProofGenerationError(msg)
                parent_index = parent_index_for(
                    index, available, rng, distribution, zipf_alpha
                )
                referenced = fk.referenced_columns[0]
                row[name] = _unique_value(referenced, column.type.name, parent_index)
                continue
            if _is_single_column_unique(table, name):
                row[name] = _unique_value(name, column.type.name, index)
                continue
            row[name] = samplers[name]()
        yield row
```

Add `from sqlproof.exceptions import SqlProofGenerationError` to the imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_bulk_rows.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/generators/bulk.py tests/unit/test_bulk_rows.py
git commit -m "feat(generators): stream bulk rows with assigned keys and skew

Primary keys come from the same _unique_value the Hypothesis path uses,
so foreign keys are satisfied by arithmetic rather than by sampling
materialised parent rows. That is what makes the path O(n)."
```

---

### Task 7: `COPY` loader and `ANALYZE`

**Files:**
- Create: `src/sqlproof/scale/__init__.py`, `src/sqlproof/scale/load.py`
- Test: `tests/integration/test_bulk_copy_live.py` (create)

**Interfaces:**
- Consumes: `bulk_table_rows` (Task 6); `resolve_insertion_plan` from `sqlproof.schema.dependency_graph`.
- Produces:
  - `copy_table(conn: psycopg.Connection, table: Table, rows: Iterable[dict[str, Any]], column_names: Sequence[str]) -> int`
  - `load_dataset(conn: psycopg.Connection, schema: SchemaInfo, sizes: Mapping[str, int], *, seed: int = 0, distribution: str = "uniform", null_frac: float = DEFAULT_NULL_FRAC) -> dict[str, int]`
  - `analyze(conn: psycopg.Connection, schema: SchemaInfo) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_bulk_copy_live.py
"""Postgres is the oracle for validity.

We never assert the two generation paths agree with each other about
what is valid -- they could be identically wrong. We assert each one
independently against a real database: generate, load with every
constraint enabled, and let Postgres reject anything invalid.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof.scale.load import analyze, load_dataset
from sqlproof.schema.parse_sql import parse_schema_sql

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE customers (
  id bigint PRIMARY KEY,
  email text NOT NULL,
  tier text
);
CREATE TABLE orders (
  id bigint PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES customers(id),
  total numeric(10,2) NOT NULL CHECK (total >= 0),
  placed_at timestamp NOT NULL
);
"""


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS bulk_test CASCADE")
        connection.execute("CREATE SCHEMA bulk_test")
        connection.execute("SET search_path TO bulk_test")
        connection.execute(SCHEMA_SQL)
        yield connection
        connection.execute("DROP SCHEMA IF EXISTS bulk_test CASCADE")


def test_bulk_load_satisfies_every_constraint(conn):
    schema = parse_schema_sql(SCHEMA_SQL, schema="bulk_test")
    counts = load_dataset(conn, schema, {"customers": 200, "orders": 2000}, seed=1)
    assert counts == {"customers": 200, "orders": 2000}
    assert conn.execute("SELECT count(*) FROM bulk_test.orders").fetchone()[0] == 2000
    # If any FK, CHECK or NOT NULL were violated, COPY would have raised.
    orphans = conn.execute(
        "SELECT count(*) FROM bulk_test.orders o "
        "LEFT JOIN bulk_test.customers c ON c.id = o.customer_id "
        "WHERE c.id IS NULL"
    ).fetchone()[0]
    assert orphans == 0


def test_analyze_populates_planner_statistics(conn):
    schema = parse_schema_sql(SCHEMA_SQL, schema="bulk_test")
    load_dataset(conn, schema, {"customers": 100, "orders": 1000}, seed=1)
    analyze(conn, schema)
    rows = conn.execute(
        "SELECT count(*) FROM pg_stats WHERE schemaname = 'bulk_test'"
    ).fetchone()[0]
    assert rows > 0


def test_load_is_linear_enough_to_reach_scale(conn):
    """50k rows must load in seconds, not the hours the Hypothesis path
    would take (projected ~3h at 500k -- see the design doc's Evidence)."""
    import time

    schema = parse_schema_sql(SCHEMA_SQL, schema="bulk_test")
    start = time.perf_counter()
    load_dataset(conn, schema, {"customers": 500, "orders": 50_000}, seed=1)
    assert time.perf_counter() - start < 60
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_bulk_copy_live.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqlproof.scale'`

- [ ] **Step 3: Implement the loader**

```python
# src/sqlproof/scale/__init__.py
"""Scale analysis: bulk loading now, sweep and fit in phase 2."""
```

```python
# src/sqlproof/scale/load.py
from __future__ import annotations

import random
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import psycopg

from sqlproof.generators.bulk import DEFAULT_NULL_FRAC, bulk_table_rows
from sqlproof.schema.dependency_graph import resolve_insertion_plan
from sqlproof.schema.model import SchemaInfo, Table


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def copy_table(
    conn: psycopg.Connection,
    table: Table,
    rows: Iterable[dict[str, Any]],
    column_names: Sequence[str],
) -> int:
    """Stream `rows` into `table` via COPY. Returns rows written."""
    qualified = f"{_quote(table.schema)}.{_quote(table.name)}"
    columns = ", ".join(_quote(c) for c in column_names)
    written = 0
    with conn.cursor() as cur, cur.copy(
        f"COPY {qualified} ({columns}) FROM STDIN"
    ) as copy:
        for row in rows:
            copy.write_row(tuple(row.get(c) for c in column_names))
            written += 1
    return written


def load_dataset(
    conn: psycopg.Connection,
    schema: SchemaInfo,
    sizes: Mapping[str, int],
    *,
    seed: int = 0,
    distribution: str = "uniform",
    null_frac: float = DEFAULT_NULL_FRAC,
) -> dict[str, int]:
    """Generate and COPY every table in FK-safe order.

    Reuses resolve_insertion_plan so parents always land before
    children -- the same ordering the Hypothesis path uses.
    """
    plan = resolve_insertion_plan(schema.tables)
    rng = random.Random(seed)
    loaded: dict[str, int] = {}
    for table in plan.ordered_tables:
        count = sizes.get(table.name, 0)
        if count <= 0:
            continue
        column_names = [
            c.name
            for c in table.columns
            if not c.is_generated and c.default is None
        ]
        rows = bulk_table_rows(
            table,
            count=count,
            rng=rng,
            parent_counts=loaded,
            distribution=distribution,
            null_frac=null_frac,
        )
        loaded[table.name] = copy_table(conn, table, rows, column_names)
    return loaded


def analyze(conn: psycopg.Connection, schema: SchemaInfo) -> None:
    """Refresh planner statistics.

    Non-optional after a bulk load: without it the planner has no
    statistics for the new rows and will choose plans that have nothing
    to do with what a user would see.
    """
    for table in schema.tables:
        conn.execute(f"ANALYZE {_quote(table.schema)}.{_quote(table.name)}")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
export SQLPROOF_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54399/postgres
uv run pytest tests/integration/test_bulk_copy_live.py -v
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/scale tests/integration/test_bulk_copy_live.py
git commit -m "feat(scale): COPY-based bulk loader with ANALYZE

Postgres is the validity oracle: a constraint violation surfaces as a
COPY failure rather than being asserted path-against-path."
```

---

### Task 8: Build out `sqlproof.testing.schemas()`

Today it discards `max_columns` and emits only single-column `id integer` tables. The differential test in Task 9 is worthless against it.

**Files:**
- Modify: `src/sqlproof/testing.py:76-99`
- Test: `tests/unit/test_schemas_strategy.py` (create)

**Interfaces:**
- Consumes: `KNOWN_TYPE_NAMES` (Task 2).
- Produces: `schemas(max_tables: int = 3, max_columns: int = 5, *, with_foreign_keys: bool = True, with_checks: bool = True) -> SearchStrategy[SchemaInfo]` — signature extended, existing positional args preserved.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_schemas_strategy.py
from __future__ import annotations

from hypothesis import HealthCheck, given, settings

from sqlproof.testing import schemas


@given(schemas(max_tables=3, max_columns=5))
@settings(max_examples=40, deadline=None, suppress_health_check=list(HealthCheck))
def test_generated_schemas_vary_in_shape(schema):
    assert 1 <= len(schema.tables) <= 3
    for table in schema.tables:
        assert 1 <= len(table.columns) <= 5
        assert table.primary_key


def test_schemas_eventually_produce_multiple_column_types():
    from hypothesis import find

    found = find(
        schemas(max_tables=1, max_columns=5),
        lambda s: len({c.type.name for c in s.tables[0].columns}) >= 2,
    )
    assert found is not None


def test_schemas_eventually_produce_a_nullable_column():
    from hypothesis import find

    found = find(
        schemas(max_tables=1, max_columns=5),
        lambda s: any(c.nullable for c in s.tables[0].columns),
    )
    assert found is not None


def test_schemas_eventually_produce_a_foreign_key():
    from hypothesis import find

    found = find(
        schemas(max_tables=3, max_columns=4),
        lambda s: any(t.foreign_keys for t in s.tables),
    )
    assert found is not None


def test_schemas_eventually_produce_a_check_constraint():
    from hypothesis import find

    found = find(
        schemas(max_tables=2, max_columns=4),
        lambda s: any(t.check_constraints for t in s.tables),
    )
    assert found is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_schemas_strategy.py -v`
Expected: FAIL — the stub emits one `id integer` column per table, so the type-variety, nullable, FK and CHECK tests all fail.

- [ ] **Step 3: Replace the stub**

Replace `schemas()` in `src/sqlproof/testing.py` entirely:

```python
_SCHEMA_COLUMN_TYPES: tuple[PgType, ...] = (
    PgType("scalar", "integer"),
    PgType("scalar", "bigint"),
    PgType("scalar", "text"),
    PgType("scalar", "boolean"),
    PgType("scalar", "numeric", modifiers=(10, 2)),
    PgType("scalar", "date"),
    PgType("scalar", "uuid"),
)


def schemas(
    max_tables: int = 3,
    max_columns: int = 5,
    *,
    with_foreign_keys: bool = True,
    with_checks: bool = True,
) -> SearchStrategy[SchemaInfo]:
    """Generate varied schemas for cross-path differential testing.

    Must produce the constructs where the two generation paths could
    disagree -- varied types, nullability, foreign keys and CHECK
    constraints. A strategy that only emits `id integer` tables cannot
    find a divergence, because it never generates anything that diverges.
    """
    names = st.lists(
        st.sampled_from(["users", "orders", "products", "scores", "events"]),
        min_size=1,
        max_size=max_tables,
        unique=True,
    )

    @st.composite
    def build(draw: st.DrawFn) -> SchemaInfo:
        table_names = draw(names)
        tables: list[Table] = []
        for position, name in enumerate(table_names):
            n_columns = draw(st.integers(min_value=1, max_value=max_columns - 1))
            columns = [Column("id", PgType("scalar", "bigint"), False, None, False)]
            checks: list[CheckConstraint] = []
            foreign_keys: list[ForeignKey] = []

            for i in range(n_columns):
                col_type = draw(st.sampled_from(_SCHEMA_COLUMN_TYPES))
                nullable = draw(st.booleans())
                col_name = f"c{i}"
                columns.append(Column(col_name, col_type, nullable, None, False))
                if (
                    with_checks
                    and not nullable
                    and col_type.name in {"integer", "bigint", "numeric"}
                    and draw(st.booleans())
                ):
                    checks.append(CheckConstraint(expression=f"{col_name} >= 0"))

            # Reference an earlier table so the FK graph stays acyclic.
            if with_foreign_keys and position > 0 and draw(st.booleans()):
                parent = table_names[draw(st.integers(0, position - 1))]
                fk_name = f"{parent}_id"
                columns.append(
                    Column(fk_name, PgType("scalar", "bigint"), False, None, False)
                )
                foreign_keys.append(
                    ForeignKey(
                        columns=(fk_name,),
                        referenced_table=parent,
                        referenced_columns=("id",),
                        on_delete="NO ACTION",
                        on_update="NO ACTION",
                    )
                )

            tables.append(
                Table(
                    schema="public",
                    name=name,
                    columns=tuple(columns),
                    primary_key=("id",),
                    foreign_keys=tuple(foreign_keys),
                    unique_constraints=(),
                    check_constraints=tuple(checks),
                )
            )
        return SchemaInfo(tables=tuple(tables))

    return build()
```

Add `CheckConstraint` and `ForeignKey` to the `sqlproof.schema.model` import at the top of `testing.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_schemas_strategy.py tests/meta -v`
Expected: PASS. `tests/meta/test_meta_properties.py` now exercises a far wider input space — if it fails, it has found a real pre-existing bug in the generator. Investigate before weakening the strategy.

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/testing.py tests/unit/test_schemas_strategy.py
git commit -m "feat(testing): make schemas() generate real schema variety

The stub discarded max_columns and emitted only single-column
`id integer` tables, so tests/meta was asserting generator properties
against a near-empty input space."
```

---

### Task 9: Differential testing — both paths load into Postgres

**Files:**
- Test: `tests/integration/test_cross_path_consistency_live.py` (create)

**Interfaces:**
- Consumes: `schemas()` (Task 8), `load_dataset` (Task 7), `dataset_strategy` and `_insert_dataset` (existing).
- Produces: nothing.

- [ ] **Step 1: Write the test**

```python
# tests/integration/test_cross_path_consistency_live.py
"""Differential test: both generation paths must produce loadable data.

Note what is NOT asserted -- that the paths produce the same values.
The bulk path deliberately draws from a narrower alphabet and does not
chase edge cases. The contract is validity, type coverage and
statistical shape, never value equality.
"""
from __future__ import annotations

import os

import psycopg
import pytest
from hypothesis import HealthCheck, given, settings

from sqlproof.scale.load import load_dataset
from sqlproof.testing import schemas

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)


def _ddl_for(schema) -> str:
    parts = []
    for table in schema.tables:
        cols = []
        for column in table.columns:
            null = "" if column.nullable else " NOT NULL"
            cols.append(f'"{column.name}" {column.type.name}{null}')
        cols.append(f'PRIMARY KEY ({", ".join(table.primary_key)})')
        for fk in table.foreign_keys:
            cols.append(
                f'FOREIGN KEY ({", ".join(fk.columns)}) REFERENCES '
                f'"{fk.referenced_table}" ({", ".join(fk.referenced_columns)})'
            )
        for check in table.check_constraints:
            cols.append(f"CHECK ({check.expression})")
        parts.append(f'CREATE TABLE "{table.name}" ({", ".join(cols)});')
    return "\n".join(parts)


@given(schema=schemas(max_tables=3, max_columns=4))
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=list(HealthCheck),
)
def test_bulk_path_produces_loadable_data_for_any_generated_schema(schema):
    sizes = {t.name: 40 for t in schema.tables}
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS xpath CASCADE")
        conn.execute("CREATE SCHEMA xpath")
        conn.execute("SET search_path TO xpath")
        try:
            conn.execute(_ddl_for(schema))
            # Postgres is the oracle. A constraint violation raises here.
            counts = load_dataset(conn, schema, sizes, seed=7)
            assert counts
            for name, expected in sizes.items():
                actual = conn.execute(f'SELECT count(*) FROM xpath."{name}"').fetchone()[0]
                assert actual == expected
        finally:
            conn.execute("DROP SCHEMA IF EXISTS xpath CASCADE")
```

- [ ] **Step 2: Run the test**

```bash
uv run pytest tests/integration/test_cross_path_consistency_live.py -v
```
Expected: PASS. A failure here is a genuine divergence — the bulk path generated something Postgres rejects. Fix the generator, never the assertion.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_cross_path_consistency_live.py
git commit -m "test(scale): differential test of bulk path over generated schemas"
```

---

### Task 10: `pg_stats` parity between the two paths

The only test in this plan that is genuinely an *equivalence* check, and the one specific to the measurement feature: a plan difference invalidates every measurement built on this, so what must match is what the planner reads.

**Files:**
- Test: `tests/integration/test_pg_stats_parity_live.py` (create)
- Possibly modify: `src/sqlproof/generators/bulk.py` (`DEFAULT_NULL_FRAC`)

**Interfaces:**
- Consumes: `load_dataset`, `analyze` (Task 7); `dataset_strategy` and `SqlProof._insert_dataset` (existing).
- Produces: a tuned `DEFAULT_NULL_FRAC`.

- [ ] **Step 1: Write the test**

```python
# tests/integration/test_pg_stats_parity_live.py
"""pg_stats is the planner's own input, which makes it the right
equivalence oracle for a feature whose output is a measurement.

If the bulk path never produces NULLs where the Hypothesis path does,
selectivity shifts, the planner may choose a different plan, and the
sweep measures something users never run.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof.core import _insert_dataset
from sqlproof.generators.graph import dataset_strategy
from sqlproof.generators.sampling import draw_example
from sqlproof.scale.load import analyze, load_dataset
from sqlproof.schema.parse_sql import parse_schema_sql

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE items (
  id bigint PRIMARY KEY,
  label text,
  qty integer NOT NULL
);
"""
N = 800


def _stats(conn, schema_name: str) -> dict[str, tuple[float, float]]:
    rows = conn.execute(
        "SELECT attname, null_frac, n_distinct FROM pg_stats "
        "WHERE schemaname = %s AND tablename = 'items'",
        (schema_name,),
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        yield connection


def _prepare(conn, name: str) -> None:
    conn.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
    conn.execute(f"CREATE SCHEMA {name}")
    conn.execute(f"SET search_path TO {name}")
    conn.execute(SCHEMA_SQL)


def test_null_fraction_is_comparable_between_paths(conn):
    schema_h = parse_schema_sql(SCHEMA_SQL, schema="parity_h")
    schema_b = parse_schema_sql(SCHEMA_SQL, schema="parity_b")
    try:
        _prepare(conn, "parity_h")
        dataset = draw_example(dataset_strategy(schema_h, sizes={"items": N}))
        _insert_dataset(_ConnClient(conn), schema_h, dataset)
        analyze(conn, schema_h)
        hypothesis_stats = _stats(conn, "parity_h")

        _prepare(conn, "parity_b")
        load_dataset(conn, schema_b, {"items": N}, seed=1)
        analyze(conn, schema_b)
        bulk_stats = _stats(conn, "parity_b")

        h_null = hypothesis_stats["label"][0]
        b_null = bulk_stats["label"][0]
        # Tolerance is generous; the point is to catch "bulk never emits
        # NULL" (0.0 vs 0.5), not to force the paths to match exactly.
        assert abs(h_null - b_null) < 0.20, (
            f"null_frac diverged: hypothesis={h_null}, bulk={b_null}. "
            "Tune DEFAULT_NULL_FRAC in generators/bulk.py."
        )
    finally:
        conn.execute("DROP SCHEMA IF EXISTS parity_h CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS parity_b CASCADE")


class _ConnClient:
    """Minimal SqlProofClient adapter so _insert_dataset can run on a
    raw psycopg connection."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def execute(self, sql: str, *params):
        self._conn.execute(sql, params or None)
        return 0
```

- [ ] **Step 2: Run the test and read the reported values**

```bash
uv run pytest tests/integration/test_pg_stats_parity_live.py -v
```
Expected: may FAIL initially. The assertion message prints both observed `null_frac` values.

- [ ] **Step 3: Tune `DEFAULT_NULL_FRAC` to the observed Hypothesis rate**

The Hypothesis path builds nullable columns as `st.one_of(st.none(), strategy)` (`columns.py:22-25`), whose NULL rate is a property of Hypothesis's `one_of` weighting rather than a number we chose. Set `DEFAULT_NULL_FRAC` in `src/sqlproof/generators/bulk.py` to the value the test reports for `hypothesis=`, rounded to two decimals, and record the observed figure in a comment:

```python
# Tuned to match the Hypothesis path's observed null_frac for nullable
# columns (st.one_of(st.none(), ...) in columns.py). Divergence here
# shifts selectivity and can change the plan, which would invalidate any
# measurement built on this data.
DEFAULT_NULL_FRAC = 0.??  # replace with the measured value
```

- [ ] **Step 4: Re-run to verify it passes**

Run: `uv run pytest tests/integration/test_pg_stats_parity_live.py -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite with coverage**

```bash
uv run pytest --cov=sqlproof --cov-fail-under=95
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_pg_stats_parity_live.py src/sqlproof/generators/bulk.py
git commit -m "test(scale): assert pg_stats parity between generation paths

pg_stats is what the planner reads, so it is the equivalence oracle
that matters for a measurement feature. Tunes DEFAULT_NULL_FRAC to the
Hypothesis path's observed null fraction."
```

---

## Self-Review Notes

**Spec coverage.** Every Phase 1 item in the spec maps to a task: type registry (2, 3, 4, 5), bulk generator (4, 6), `COPY` loader and `ANALYZE` (7), prerequisite `schemas()` (8), prerequisite `rows.py` hoisting (1), and the four consistency backstops — structural (2, 3, 4), exhaustiveness (5), Postgres-as-oracle (7, 9), differential (9), `pg_stats` parity (10). Phase 2 items (probe, sweep, fit, artifact, CLI, advisor ranking) are deliberately absent.

**Known gaps to resolve during execution, not before:**

1. **Composite primary keys.** `bulk_table_rows` handles single-column PKs via `_unique_value` and skips composite ones (`single_pk` is `None`). The Hypothesis path handles composites through `_composite_unique_keys`. Task 9's differential test only generates single-column PKs, so it will not catch this. If a real schema needs composite PKs, that is a follow-up task — record it rather than silently generating invalid data; `bulk_table_rows` should raise a clear `SqlProofGenerationError` for composite PKs until then.
2. **CHECK constraints in the bulk path.** Task 6 does not run generated values through `refine_for_checks`. Task 8 generates `c >= 0` CHECKs and Task 9 loads against them, so this *will* fail on the first negative integer. The fix belongs in Task 6's implementation: either reuse the refinement pipeline, or constrain the sampler's bounds from the parsed check. Expect to iterate here — this is the most likely place execution stalls, and it is deliberately surfaced rather than hidden.
3. **`parent_index_for`'s Zipf implementation** uses `paretovariate` as a stand-in; `random.Random` has no `zipfvariate`. Verify the distribution empirically against the Task 6 test rather than trusting the formula.
4. **Deferred FK cycles.** `load_dataset` follows `resolve_insertion_plan`'s ordering but does not implement the second UPDATE pass that `core.py:346-391` does for deferred edges. Cyclic schemas will fail. Task 8's `schemas()` only generates acyclic FK graphs (it references earlier tables only), so this is not exercised. Follow-up.
5. **FK key derivation uses the child column's type name.** In Task 6, `_unique_value(referenced, column.type.name, parent_index)` derives the parent's key using the *child* FK column's type. Postgres requires FK column types to be compatible, so this is correct in practice for every type `_unique_value` distinguishes (the integer family collapses to the same value). It is still a latent trap if the two ever diverge — prefer passing the parent table's PK column type once `load_dataset` has the parent `Table` to hand.

**Type consistency.** `TypeSpec`, `spec_for_type`, `spec_for_column`, `strategy_for_spec`, `sampler_for_spec`, `sampler_for_column`, `bulk_table_rows`, `parent_index_for`, `copy_table`, `load_dataset` and `analyze` are used with identical signatures everywhere they appear across tasks.
