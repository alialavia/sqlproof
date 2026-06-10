# Mutation Testing Harness v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Ship `run_mutation_tests()` — apply deliberate bugs to SQL function bodies, run the user's pytest suite against each on a fresh clone database, and report every mutant the suite failed to kill.

**Architecture:** A new `src/sqlproof/mutation/` package. Mutants are serializable `(target, text ops)` artifacts authored via `MutationSet.for_function`. Preparation (extract function from schema DDL with pglast → apply text ops → AST-validate → build `CREATE OR REPLACE FUNCTION` DDL via AST splice + `RawStream` deparse) happens eagerly with zero database access. Execution clones a template database per mutant (`CREATE DATABASE ... TEMPLATE` — no restore ever), applies the mutant DDL, runs pytest as a subprocess with a DSN env-var override, and maps the exit code to an outcome.

**Tech Stack:** Python 3.11+, pglast (already a dependency — parse, `pglast.ast` node mutation, `RawStream` deparse, `parse_plpgsql`), psycopg, subprocess pytest.

**Spec:** `docs/superpowers/specs/2026-06-10-mutation-testing-design.md` (on branch `docs/mutation-testing-design`, PR #88). Refs issue #11.

**Deliberate v1 deferrals** (from the spec, to keep this plan independently shippable):
- `MutationSet.for_policy` (RLS policies) — follow-up plan; the `Mutant.target_kind` field is designed for it.
- The capped-Hypothesis-examples default profile — v1 records/pins the seed via `--hypothesis-seed`; example budgets stay in the user's `pytest_args`.
- A formal `MutationRunner` protocol — `LocalMutationRunner`'s overridable methods are the seam; extract a protocol when the cloud runner exists.
- The static AST mutation catalog and LLM-proposed mutants.

**Conventions (verified against this codebase):**
- Frozen slotted dataclasses (`@dataclass(frozen=True, slots=True)`), `from __future__ import annotations` first line in every module.
- Errors subclass `SqlProofError` in `src/sqlproof/exceptions.py` with one-line docstrings.
- pglast nodes are typed as `Any` (see `schema/parse_sql.py`); `RawStream()` needs `# type: ignore[no-untyped-call]`.
- Integration tests live in `tests/integration/`, gated with `@pytest.mark.skipif("SQLPROOF_TEST_DATABASE_URL" not in os.environ, reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests")`.
- Verification commands (CI parity): `uv run pytest tests/unit -q`, `uv run ruff check src/ tests/`, `uv run pyright`, `uv run mypy src/sqlproof/`.
- Conventional commits: `feat(mutation): ...`, `test(mutation): ...`.

---

### Task 0: Commit the plan

**Files:**
- Create: `docs/superpowers/plans/2026-06-10-mutation-testing-v1.md` (this file)

- [x] **Step 1: Commit**

```bash
git add docs/superpowers/plans/2026-06-10-mutation-testing-v1.md
git commit -m "docs(plan): mutation testing harness v1 implementation plan"
```

---

### Task 1: Exception type + mutant model + JSON round-trip

**Files:**
- Modify: `src/sqlproof/exceptions.py` (append at end)
- Create: `src/sqlproof/mutation/__init__.py`
- Create: `src/sqlproof/mutation/model.py`
- Test: `tests/unit/test_mutation_model.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/unit/test_mutation_model.py
from __future__ import annotations

import pytest

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.model import Drop, Mutant, MutationSet, Replace


def test_for_function_creates_one_mutant_per_op() -> None:
    mutations = MutationSet.for_function(
        "get_user_usage_total",
        [
            Replace("feature = p_feature", "feature <> p_feature"),
            Drop("WHERE user_id = p_user_id"),
        ],
    )
    assert len(mutations.mutants) == 2
    first, second = mutations.mutants
    assert first.target_kind == "function"
    assert first.target_name == "get_user_usage_total"
    assert first.ops == (Replace("feature = p_feature", "feature <> p_feature"),)
    assert second.ops == (Drop("WHERE user_id = p_user_id"),)


def test_for_function_rejects_empty_ops() -> None:
    with pytest.raises(SqlProofMutationError, match="at least one"):
        MutationSet.for_function("f", [])


def test_replace_rejects_identical_old_and_new() -> None:
    with pytest.raises(SqlProofMutationError, match="no-op"):
        Replace("x", "x")


def test_expect_survives_requires_reason() -> None:
    with pytest.raises(SqlProofMutationError, match="reason"):
        Replace("a", "b", expect_survives=True)
    with pytest.raises(SqlProofMutationError, match="reason"):
        Drop("a", expect_survives=True)


def test_reason_requires_expect_survives() -> None:
    with pytest.raises(SqlProofMutationError, match="expect_survives"):
        Replace("a", "b", reason="dead code")


def test_expect_survives_is_lifted_onto_the_mutant() -> None:
    mutations = MutationSet.for_function(
        "f", [Drop("AND deleted_at IS NULL", expect_survives=True, reason="dead branch")]
    )
    (mutant,) = mutations.mutants
    assert mutant.expect_survives is True
    assert mutant.reason == "dead branch"


def test_mutation_sets_concatenate() -> None:
    a = MutationSet.for_function("f", [Drop("x")])
    b = MutationSet.for_function("g", [Drop("y")])
    combined = a + b
    assert [m.target_name for m in combined.mutants] == ["f", "g"]


def test_json_round_trip() -> None:
    mutations = MutationSet.for_function(
        "get_user_usage_total",
        [
            Replace("used_at >= p_start", "used_at > p_start"),
            Drop("AND deleted_at IS NULL", expect_survives=True, reason="dead branch"),
        ],
    )
    data = mutations.to_dict()
    restored = MutationSet.from_dict(data)
    assert restored == mutations
    # The wire format is plain JSON-able primitives.
    import json

    assert json.loads(json.dumps(data)) == data


def test_describe_is_human_readable() -> None:
    mutant = MutationSet.for_function("f", [Replace("a", "b")]).mutants[0]
    assert "f" in mutant.describe()
    assert "'a'" in mutant.describe()
    assert "'b'" in mutant.describe()
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_mutation_model.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqlproof.mutation'` (and `ImportError` for `SqlProofMutationError`).

- [x] **Step 3: Implement**

Append to `src/sqlproof/exceptions.py`:

```python
class SqlProofMutationError(SqlProofError):
    """Mutation testing: bad mutant definition, apply failure, or surviving mutants."""
```

Create `src/sqlproof/mutation/__init__.py`:

```python
from __future__ import annotations

from sqlproof.mutation.model import Drop, Mutant, MutationSet, Replace

__all__ = ["Drop", "Mutant", "MutationSet", "Replace"]
```

Create `src/sqlproof/mutation/model.py`:

```python
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from sqlproof.exceptions import SqlProofMutationError


def _check_survivor_fields(expect_survives: bool, reason: str | None) -> None:
    if expect_survives and not reason:
        msg = "expect_survives=True requires a reason= explaining the accepted survivor."
        raise SqlProofMutationError(msg)
    if reason and not expect_survives:
        msg = "reason= is only meaningful with expect_survives=True."
        raise SqlProofMutationError(msg)


@dataclass(frozen=True, slots=True)
class Replace:
    """Replace exactly one occurrence of `old` with `new` in the target body."""

    old: str
    new: str
    expect_survives: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.old == self.new:
            msg = f"Replace is a no-op: old and new are both {self.old!r}."
            raise SqlProofMutationError(msg)
        _check_survivor_fields(self.expect_survives, self.reason)

    def describe(self) -> str:
        return f"replace {self.old!r} -> {self.new!r}"

    def to_dict(self) -> dict[str, Any]:
        return {"op": "replace", "old": self.old, "new": self.new}


@dataclass(frozen=True, slots=True)
class Drop:
    """Delete exactly one occurrence of `pattern` from the target body."""

    pattern: str
    expect_survives: bool = False
    reason: str | None = None

    def __post_init__(self) -> None:
        _check_survivor_fields(self.expect_survives, self.reason)

    def describe(self) -> str:
        return f"drop {self.pattern!r}"

    def to_dict(self) -> dict[str, Any]:
        return {"op": "drop", "pattern": self.pattern}


Op = Replace | Drop


def _op_from_dict(data: dict[str, Any]) -> Op:
    kind = data.get("op")
    if kind == "replace":
        return Replace(data["old"], data["new"])
    if kind == "drop":
        return Drop(data["pattern"])
    msg = f"Unknown mutation op kind: {kind!r}."
    raise SqlProofMutationError(msg)


@dataclass(frozen=True, slots=True)
class Mutant:
    """One deliberate bug: a target plus ordered text operations on its body."""

    target_kind: Literal["function"]
    target_name: str
    ops: tuple[Op, ...]
    expect_survives: bool = False
    reason: str | None = None

    def describe(self) -> str:
        rendered = "; ".join(op.describe() for op in self.ops)
        return f"{self.target_name}: {rendered}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": {"kind": self.target_kind, "name": self.target_name},
            "ops": [op.to_dict() for op in self.ops],
            "expect_survives": self.expect_survives,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Mutant:
        target = data["target"]
        if target["kind"] != "function":
            msg = f"Unknown mutant target kind: {target['kind']!r}."
            raise SqlProofMutationError(msg)
        return cls(
            target_kind="function",
            target_name=target["name"],
            ops=tuple(_op_from_dict(op) for op in data["ops"]),
            expect_survives=bool(data.get("expect_survives", False)),
            reason=data.get("reason"),
        )


@dataclass(frozen=True, slots=True)
class MutationSet:
    mutants: tuple[Mutant, ...]

    @classmethod
    def for_function(cls, name: str, ops: Sequence[Op]) -> MutationSet:
        """One mutant per op against the named function's body."""
        if not ops:
            msg = f"MutationSet.for_function({name!r}) needs at least one op."
            raise SqlProofMutationError(msg)
        mutants = tuple(
            Mutant(
                target_kind="function",
                target_name=name,
                ops=(op,),
                expect_survives=op.expect_survives,
                reason=op.reason,
            )
            for op in ops
        )
        return cls(mutants)

    def __add__(self, other: MutationSet) -> MutationSet:
        return MutationSet(self.mutants + other.mutants)

    def to_dict(self) -> dict[str, Any]:
        return {"mutants": [mutant.to_dict() for mutant in self.mutants]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MutationSet:
        return cls(tuple(Mutant.from_dict(item) for item in data["mutants"]))
```

Note: `Replace`/`Drop` carry `expect_survives`/`reason` purely as authoring sugar — `for_function` lifts them onto the `Mutant`, which is the field of record and the only place they serialize. Because dataclass equality includes those flags, `for_function` must store flag-stripped copies of the ops so the JSON round-trip compares equal. Add this helper to `model.py` and use `ops=(_bare(op),)` inside `for_function` (replacing the bare `ops=(op,)` shown above):

```python
def _bare(op: Op) -> Op:
    """Strip authoring-sugar flags; the Mutant carries them instead."""
    if isinstance(op, Replace):
        return Replace(op.old, op.new)
    return Drop(op.pattern)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_model.py -q`
Expected: PASS (9 tests).

- [x] **Step 5: Lint and commit**

```bash
uv run ruff check src/sqlproof/mutation/ src/sqlproof/exceptions.py tests/unit/test_mutation_model.py
git add src/sqlproof/mutation/ src/sqlproof/exceptions.py tests/unit/test_mutation_model.py
git commit -m "feat(mutation): mutant model, MutationSet.for_function, JSON round-trip"
```

---

### Task 2: Extract function source from schema DDL (pglast)

The file-based schema parser (`schema/parse_sql.py`) ignores `CREATE FUNCTION` entirely — `SchemaInfo.functions` is only populated by live introspection. The mutation package therefore does its own extraction.

**Files:**
- Create: `src/sqlproof/mutation/extract.py`
- Test: `tests/unit/test_mutation_extract.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/unit/test_mutation_extract.py
from __future__ import annotations

import pytest

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.extract import extract_function

SCHEMA_SQL = """
CREATE TABLE usage_events (
    id serial PRIMARY KEY,
    user_id integer NOT NULL,
    amount integer NOT NULL
);

CREATE FUNCTION total_usage(p_user integer) RETURNS bigint
LANGUAGE sql STABLE
AS $$
    SELECT COALESCE(SUM(amount), 0) FROM usage_events WHERE user_id = p_user
$$;

CREATE FUNCTION bump(p_id integer) RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE usage_events SET amount = amount + 1 WHERE id = p_id;
END;
$$;
"""


def test_extracts_sql_function_body_and_language() -> None:
    source = extract_function(SCHEMA_SQL, "total_usage")
    assert source.name == "total_usage"
    assert source.language == "sql"
    assert "COALESCE(SUM(amount), 0)" in source.body
    assert "WHERE user_id = p_user" in source.body


def test_extracts_plpgsql_function() -> None:
    source = extract_function(SCHEMA_SQL, "bump")
    assert source.language == "plpgsql"
    assert "amount + 1" in source.body


def test_ddl_is_a_deparsed_create_statement() -> None:
    source = extract_function(SCHEMA_SQL, "total_usage")
    assert source.ddl.upper().startswith("CREATE FUNCTION")
    assert "total_usage" in source.ddl


def test_missing_function_raises() -> None:
    with pytest.raises(SqlProofMutationError, match="no_such_function"):
        extract_function(SCHEMA_SQL, "no_such_function")


def test_overloaded_function_raises_ambiguous() -> None:
    overloaded = (
        SCHEMA_SQL
        + "\nCREATE FUNCTION total_usage(p_user integer, p_cap integer) RETURNS bigint"
        + "\nLANGUAGE sql AS $$ SELECT 1 $$;"
    )
    with pytest.raises(SqlProofMutationError, match="2 definitions"):
        extract_function(overloaded, "total_usage")


def test_unparseable_schema_raises() -> None:
    with pytest.raises(SqlProofMutationError, match="does not parse"):
        extract_function("CREATE FUNCTION oops(", "oops")


def test_begin_atomic_body_is_rejected_for_now() -> None:
    atomic = """
    CREATE FUNCTION one() RETURNS integer LANGUAGE sql
    BEGIN ATOMIC
        SELECT 1;
    END;
    """
    with pytest.raises(SqlProofMutationError, match="BEGIN ATOMIC"):
        extract_function(atomic, "one")
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_mutation_extract.py -q`
Expected: FAIL — `ModuleNotFoundError` for `sqlproof.mutation.extract`.

- [x] **Step 3: Implement**

Create `src/sqlproof/mutation/extract.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pglast import parse_sql as parse_postgres_sql
from pglast.stream import RawStream

from sqlproof.exceptions import SqlProofMutationError


@dataclass(frozen=True, slots=True)
class FunctionSource:
    """A CREATE FUNCTION statement located in schema DDL.

    `body` is the AS $$ ... $$ string verbatim; `ddl` is the whole
    statement, deparsed from the AST (so formatting is canonical).
    """

    name: str
    language: str
    body: str
    ddl: str


def _function_statements(schema_sql: str) -> list[Any]:
    try:
        raw_statements: tuple[Any, ...] = tuple(parse_postgres_sql(schema_sql))
    except Exception as exc:
        msg = f"Schema SQL does not parse: {exc}"
        raise SqlProofMutationError(msg) from exc
    return [
        raw.stmt
        for raw in raw_statements
        if type(raw.stmt).__name__ == "CreateFunctionStmt"
    ]


def _matching_statement(schema_sql: str, name: str) -> Any:
    matches = [
        statement
        for statement in _function_statements(schema_sql)
        if statement.funcname[-1].sval == name
    ]
    if not matches:
        msg = f"No CREATE FUNCTION for {name!r} found in the schema SQL."
        raise SqlProofMutationError(msg)
    if len(matches) > 1:
        msg = (
            f"Function {name!r} is ambiguous: {len(matches)} definitions found "
            "(overloads are not supported yet)."
        )
        raise SqlProofMutationError(msg)
    statement = matches[0]
    if getattr(statement, "sql_body", None) is not None:
        msg = (
            f"Function {name!r} uses a BEGIN ATOMIC body, which is not supported "
            "yet; define it with AS $$ ... $$ instead."
        )
        raise SqlProofMutationError(msg)
    return statement


def _option(statement: Any, defname: str, function_name: str) -> Any:
    for option in statement.options or ():
        if option.defname == defname:
            return option
    msg = f"Function {function_name!r} has no {defname!r} clause."
    raise SqlProofMutationError(msg)


def _body_from_statement(statement: Any, function_name: str) -> str:
    as_option = _option(statement, "as", function_name)
    items: tuple[Any, ...] = tuple(as_option.arg.items)
    if len(items) != 1:
        msg = (
            f"Function {function_name!r} has a multi-part AS clause "
            "(C-language functions are not supported)."
        )
        raise SqlProofMutationError(msg)
    return str(items[0].sval)


def extract_function(schema_sql: str, name: str) -> FunctionSource:
    statement = _matching_statement(schema_sql, name)
    language = str(_option(statement, "language", name).arg.sval)
    body = _body_from_statement(statement, name)
    stream = RawStream()  # type: ignore[no-untyped-call]
    return FunctionSource(name=name, language=language, body=body, ddl=stream(statement))
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_extract.py -q`
Expected: PASS (7 tests).

If `test_begin_atomic_body_is_rejected_for_now` fails because pglast represents the BEGIN ATOMIC body differently (e.g. attribute named `sql_body` is always present but `None`): inspect with `python -c "from pglast import parse_sql; print(parse_sql('CREATE FUNCTION one() RETURNS integer LANGUAGE sql BEGIN ATOMIC SELECT 1; END;')[0].stmt)"` and adjust the guard — the contract under test (a clear `SqlProofMutationError` mentioning BEGIN ATOMIC) must hold.

- [x] **Step 5: Lint and commit**

```bash
uv run ruff check src/sqlproof/mutation/extract.py tests/unit/test_mutation_extract.py
git add src/sqlproof/mutation/extract.py tests/unit/test_mutation_extract.py
git commit -m "feat(mutation): extract function source from schema DDL via pglast"
```

---

### Task 3: Build mutated DDL via AST splice + deparse

**Files:**
- Modify: `src/sqlproof/mutation/extract.py` (append)
- Test: `tests/unit/test_mutation_extract.py` (append)

- [x] **Step 1: Write the failing tests**

Append to `tests/unit/test_mutation_extract.py`:

```python
from sqlproof.mutation.extract import build_mutated_ddl


def test_build_mutated_ddl_splices_body_and_uses_or_replace() -> None:
    mutated_body = "\n    SELECT COALESCE(SUM(amount), 1) FROM usage_events WHERE user_id = p_user\n"
    ddl = build_mutated_ddl(SCHEMA_SQL, "total_usage", mutated_body)
    assert "OR REPLACE" in ddl.upper()
    assert "COALESCE(SUM(amount), 1)" in ddl
    assert "COALESCE(SUM(amount), 0)" not in ddl


def test_build_mutated_ddl_round_trips_through_pglast() -> None:
    from pglast import parse_sql

    ddl = build_mutated_ddl(SCHEMA_SQL, "total_usage", " SELECT 42 ")
    (raw,) = parse_sql(ddl)
    assert type(raw.stmt).__name__ == "CreateFunctionStmt"
    assert raw.stmt.replace is True
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_mutation_extract.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_mutated_ddl'`.

- [x] **Step 3: Implement**

Append to `src/sqlproof/mutation/extract.py` (add `from pglast import ast as pg_ast` to the imports):

```python
def build_mutated_ddl(schema_sql: str, name: str, mutated_body: str) -> str:
    """CREATE OR REPLACE FUNCTION statement with `mutated_body` spliced in.

    Re-parses the schema and mutates the AST (pglast nodes are mutable),
    then deparses — no text splicing, so dollar-quoting and clause order
    are the deparser's problem, not ours.
    """
    statement = _matching_statement(schema_sql, name)
    statement.replace = True
    as_option = _option(statement, "as", name)
    as_option.arg = pg_ast.List(items=(pg_ast.String(sval=mutated_body),))
    stream = RawStream()  # type: ignore[no-untyped-call]
    return stream(statement)
```

If attribute assignment on the pglast node raises (older pglast versions had read-only nodes): rebuild instead — construct a new `pg_ast.CreateFunctionStmt(**{...existing fields..., "replace": True, "options": patched_options})` from the original node's attributes. Current pglast 6/7 nodes are mutable, so the straight assignment should work; verify before reaching for the rebuild.

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_extract.py -q`
Expected: PASS (9 tests).

- [x] **Step 5: Lint and commit**

```bash
uv run ruff check src/sqlproof/mutation/extract.py tests/unit/test_mutation_extract.py
git add src/sqlproof/mutation/extract.py tests/unit/test_mutation_extract.py
git commit -m "feat(mutation): build CREATE OR REPLACE DDL via AST splice and deparse"
```

---

### Task 4: Apply text ops + validate + prepare mutants

**Files:**
- Create: `src/sqlproof/mutation/apply.py`
- Test: `tests/unit/test_mutation_apply.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/unit/test_mutation_apply.py
from __future__ import annotations

import pytest

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.apply import apply_op, prepare_mutants
from sqlproof.mutation.model import Drop, MutationSet, Replace

SCHEMA_SQL = """
CREATE TABLE usage_events (
    id serial PRIMARY KEY,
    user_id integer NOT NULL,
    amount integer NOT NULL
);

CREATE FUNCTION total_usage(p_user integer) RETURNS bigint
LANGUAGE sql STABLE
AS $$
    SELECT COALESCE(SUM(amount), 0) FROM usage_events WHERE user_id = p_user
$$;

CREATE FUNCTION bump(p_id integer) RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE usage_events SET amount = amount + 1 WHERE id = p_id;
END;
$$;
"""


def test_apply_op_replaces_single_occurrence() -> None:
    assert apply_op("a AND b", Replace("AND", "OR")) == "a OR b"


def test_apply_op_drop_deletes_pattern() -> None:
    assert apply_op("SELECT 1 WHERE x", Drop(" WHERE x")) == "SELECT 1"


def test_apply_op_pattern_absent_is_loud() -> None:
    with pytest.raises(SqlProofMutationError, match="not found"):
        apply_op("SELECT 1", Replace("WHERE", "HAVING"))


def test_apply_op_pattern_ambiguous_is_loud() -> None:
    with pytest.raises(SqlProofMutationError, match="2 times"):
        apply_op("a = b AND c = d", Replace("=", "<>"))


def test_prepare_builds_or_replace_ddl_per_mutant() -> None:
    mutations = MutationSet.for_function(
        "total_usage",
        [
            Replace("COALESCE(SUM(amount), 0)", "COALESCE(SUM(amount), 1)"),
            Replace("user_id = p_user", "user_id <> p_user"),
        ],
    )
    prepared = prepare_mutants(mutations, SCHEMA_SQL)
    assert len(prepared) == 2
    assert all("OR REPLACE" in p.ddl.upper() for p in prepared)
    assert "COALESCE(SUM(amount), 1)" in prepared[0].ddl
    assert "user_id <> p_user" in prepared[1].ddl


def test_prepare_validates_sql_body_parses() -> None:
    mutations = MutationSet.for_function(
        "total_usage", [Replace("WHERE user_id", "WHRE user_id")]
    )
    with pytest.raises(SqlProofMutationError, match="does not parse"):
        prepare_mutants(mutations, SCHEMA_SQL)


def test_prepare_rejects_ast_no_op() -> None:
    # Whitespace-only change: different text, identical AST.
    mutations = MutationSet.for_function(
        "total_usage", [Replace("COALESCE(SUM(amount), 0)", "COALESCE( SUM(amount) , 0 )")]
    )
    with pytest.raises(SqlProofMutationError, match="no-op"):
        prepare_mutants(mutations, SCHEMA_SQL)


def test_prepare_validates_plpgsql_body() -> None:
    mutations = MutationSet.for_function("bump", [Replace("UPDATE", "UPDAT")])
    with pytest.raises(SqlProofMutationError, match="does not parse"):
        prepare_mutants(mutations, SCHEMA_SQL)


def test_prepare_accepts_valid_plpgsql_mutant() -> None:
    mutations = MutationSet.for_function("bump", [Replace("amount + 1", "amount + 2")])
    (prepared,) = prepare_mutants(mutations, SCHEMA_SQL)
    assert "amount + 2" in prepared.ddl


def test_mutant_ids_are_stable_and_distinct() -> None:
    mutations = MutationSet.for_function(
        "total_usage",
        [
            Replace("user_id = p_user", "user_id <> p_user"),
            Replace("COALESCE(SUM(amount), 0)", "COALESCE(SUM(amount), 1)"),
        ],
    )
    first = prepare_mutants(mutations, SCHEMA_SQL)
    second = prepare_mutants(mutations, SCHEMA_SQL)
    assert [p.mutant_id for p in first] == [p.mutant_id for p in second]
    assert len({p.mutant_id for p in first}) == 2


def test_duplicate_mutants_are_rejected() -> None:
    # Two ops that produce the identical mutated AST.
    mutations = MutationSet.for_function(
        "total_usage",
        [
            Replace("user_id = p_user", "user_id <> p_user"),
            Replace("= p_user", "<> p_user"),
        ],
    )
    with pytest.raises(SqlProofMutationError, match="duplicate"):
        prepare_mutants(mutations, SCHEMA_SQL)
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_mutation_apply.py -q`
Expected: FAIL — `ModuleNotFoundError` for `sqlproof.mutation.apply`.

- [x] **Step 3: Implement**

Create `src/sqlproof/mutation/apply.py`:

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from pglast import parse_sql as parse_postgres_sql
from pglast.parser import parse_plpgsql

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.extract import (
    FunctionSource,
    build_mutated_ddl,
    extract_function,
)
from sqlproof.mutation.model import Drop, Mutant, MutationSet, Op, Replace


@dataclass(frozen=True, slots=True)
class PreparedMutant:
    """A validated mutant, ready to run: no further parsing can fail."""

    mutant: Mutant
    mutant_id: str
    ddl: str


def apply_op(body: str, op: Op) -> str:
    if isinstance(op, Replace):
        pattern, replacement = op.old, op.new
    else:
        pattern, replacement = op.pattern, ""
    count = body.count(pattern)
    if count == 0:
        msg = f"{op.describe()}: pattern not found in the function body."
        raise SqlProofMutationError(msg)
    if count > 1:
        msg = (
            f"{op.describe()}: pattern occurs {count} times; "
            "extend it until it is unique."
        )
        raise SqlProofMutationError(msg)
    return body.replace(pattern, replacement, 1)


def _sql_ast_key(sql: str, *, context: str) -> str:
    try:
        return repr(parse_postgres_sql(sql))
    except Exception as exc:
        msg = f"{context}: mutated body does not parse — authoring error: {exc}"
        raise SqlProofMutationError(msg) from exc


def _plpgsql_ast_key(ddl: str, *, context: str) -> str:
    try:
        return json.dumps(parse_plpgsql(ddl), sort_keys=True)
    except Exception as exc:
        msg = f"{context}: mutated body does not parse — authoring error: {exc}"
        raise SqlProofMutationError(msg) from exc


def _ast_keys(
    source: FunctionSource,
    mutated_body: str,
    mutated_ddl: str,
    *,
    context: str,
) -> tuple[str, str]:
    """(original, mutated) canonical keys for no-op detection and identity."""
    if source.language == "sql":
        return (
            repr(parse_postgres_sql(source.body)),
            _sql_ast_key(mutated_body, context=context),
        )
    if source.language == "plpgsql":
        return (
            _plpgsql_ast_key(source.ddl, context=f"{context} (original)"),
            _plpgsql_ast_key(mutated_ddl, context=context),
        )
    # Unknown language: no parser available; fall back to
    # whitespace-normalized text so no-op detection still works.
    return (" ".join(source.body.split()), " ".join(mutated_body.split()))


def prepare_mutants(mutations: MutationSet, schema_sql: str) -> tuple[PreparedMutant, ...]:
    """Validate every mutant eagerly — all authoring errors surface before
    any database is touched."""
    prepared: list[PreparedMutant] = []
    seen: dict[str, str] = {}
    for mutant in mutations.mutants:
        context = mutant.describe()
        source = extract_function(schema_sql, mutant.target_name)
        body = source.body
        for op in mutant.ops:
            body = apply_op(body, op)
        ddl = build_mutated_ddl(schema_sql, mutant.target_name, body)
        original_key, mutated_key = _ast_keys(source, body, ddl, context=context)
        if original_key == mutated_key:
            msg = f"{context}: mutation is a no-op (identical AST after parsing)."
            raise SqlProofMutationError(msg)
        digest = hashlib.sha256(
            f"{mutant.target_name}\n{mutated_key}".encode()
        ).hexdigest()[:16]
        if digest in seen:
            msg = (
                f"{context}: duplicate of mutant {seen[digest]!r} "
                "(both produce the same mutated AST)."
            )
            raise SqlProofMutationError(msg)
        seen[digest] = context
        prepared.append(PreparedMutant(mutant=mutant, mutant_id=digest, ddl=ddl))
    return tuple(prepared)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_apply.py -q`
Expected: PASS (11 tests).

Likely friction point: `parse_plpgsql` exists in `pglast.parser` (it wraps libpg_query's plpgsql parser) and takes the full `CREATE FUNCTION` DDL. If the plpgsql syntax-error test fails because `parse_plpgsql` tolerates `UPDAT` (the plpgsql parser defers some statement parsing to runtime), change that test's broken mutant to one the parser does reject (e.g. `Replace("END;", "ENDD;")` — mangle block structure, which it must parse) and add a comment explaining plpgsql's lazy statement parsing.

- [x] **Step 5: Lint and commit**

```bash
uv run ruff check src/sqlproof/mutation/apply.py tests/unit/test_mutation_apply.py
git add src/sqlproof/mutation/apply.py tests/unit/test_mutation_apply.py
git commit -m "feat(mutation): apply text ops, AST validation, no-op and duplicate rejection"
```

---

### Task 5: Outcome and result model

**Files:**
- Create: `src/sqlproof/mutation/result.py`
- Test: `tests/unit/test_mutation_result.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/unit/test_mutation_result.py
from __future__ import annotations

import pytest

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.result import MutantOutcome, MutationResult, outcome_for_exit_code


def _outcome(status: str, *, mutant_id: str = "abc123") -> MutantOutcome:
    return MutantOutcome(
        mutant_id=mutant_id,
        target="total_usage",
        description=f"total_usage: drop {mutant_id!r}",
        status=status,  # type: ignore[arg-type]
        pytest_exit_code=0,
        hypothesis_seed=None,
        detail=None,
    )


@pytest.mark.parametrize(
    ("exit_code", "expect_survives", "status"),
    [
        (1, False, "killed"),
        (0, False, "survived"),
        (0, True, "expected_survivor"),
        (1, True, "unexpected_kill"),
        (2, False, "error"),
        (3, False, "error"),
        (4, False, "error"),
        (5, False, "error"),  # no tests collected proves nothing
    ],
)
def test_exit_code_mapping(exit_code: int, expect_survives: bool, status: str) -> None:
    outcome = outcome_for_exit_code(
        mutant_id="abc",
        target="f",
        description="f: drop 'x'",
        expect_survives=expect_survives,
        exit_code=exit_code,
        hypothesis_seed=7,
        detail="tail",
    )
    assert outcome.status == status
    assert outcome.pytest_exit_code == exit_code
    assert outcome.hypothesis_seed == 7


def test_assert_no_survivors_passes_when_all_killed() -> None:
    result = MutationResult(outcomes=(_outcome("killed"), _outcome("expected_survivor")))
    result.assert_no_survivors()  # must not raise


def test_assert_no_survivors_raises_on_survivor_with_description() -> None:
    result = MutationResult(outcomes=(_outcome("survived"),))
    with pytest.raises(SqlProofMutationError, match="drop"):
        result.assert_no_survivors()


def test_assert_no_survivors_raises_on_error_outcomes() -> None:
    result = MutationResult(outcomes=(_outcome("error"),))
    with pytest.raises(SqlProofMutationError, match="error"):
        result.assert_no_survivors()


def test_unexpected_kill_does_not_fail_the_run() -> None:
    # Tests now cover what was declared dead code — good news, not failure.
    result = MutationResult(outcomes=(_outcome("unexpected_kill"),))
    result.assert_no_survivors()


def test_to_dict_is_json_serializable() -> None:
    import json

    result = MutationResult(outcomes=(_outcome("killed"), _outcome("survived")))
    assert json.loads(json.dumps(result.to_dict()))["outcomes"][1]["status"] == "survived"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_mutation_result.py -q`
Expected: FAIL — `ModuleNotFoundError` for `sqlproof.mutation.result`.

- [x] **Step 3: Implement**

Create `src/sqlproof/mutation/result.py`:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from sqlproof.exceptions import SqlProofMutationError

Status = Literal["killed", "survived", "expected_survivor", "unexpected_kill", "error"]


@dataclass(frozen=True, slots=True)
class MutantOutcome:
    mutant_id: str
    target: str
    description: str
    status: Status
    pytest_exit_code: int | None
    hypothesis_seed: int | None
    detail: str | None


def outcome_for_exit_code(
    *,
    mutant_id: str,
    target: str,
    description: str,
    expect_survives: bool,
    exit_code: int,
    hypothesis_seed: int | None,
    detail: str | None,
) -> MutantOutcome:
    """pytest exit codes: 0 all passed, 1 tests failed, 2 interrupted,
    3 internal error, 4 usage error, 5 no tests collected. Only 0 and 1
    are evidence about the mutant; everything else is an error."""
    if exit_code == 1:
        status: Status = "unexpected_kill" if expect_survives else "killed"
    elif exit_code == 0:
        status = "expected_survivor" if expect_survives else "survived"
    else:
        status = "error"
    return MutantOutcome(
        mutant_id=mutant_id,
        target=target,
        description=description,
        status=status,
        pytest_exit_code=exit_code,
        hypothesis_seed=hypothesis_seed,
        detail=detail,
    )


@dataclass(frozen=True, slots=True)
class MutationResult:
    outcomes: tuple[MutantOutcome, ...]

    @property
    def survivors(self) -> tuple[MutantOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == "survived")

    @property
    def errors(self) -> tuple[MutantOutcome, ...]:
        return tuple(o for o in self.outcomes if o.status == "error")

    def assert_no_survivors(self) -> None:
        """Fail on surviving mutants AND on errored runs — a mutant whose
        suite run errored proves nothing about test strength."""
        problems = [*self.survivors, *self.errors]
        if not problems:
            return
        lines = [f"{len(problems)} mutant(s) not killed:"]
        for outcome in problems:
            lines.append(f"  [{outcome.status}] {outcome.description}")
            if outcome.detail:
                lines.append(f"    {outcome.detail.strip()[-500:]}")
        raise SqlProofMutationError("\n".join(lines))

    def to_dict(self) -> dict[str, Any]:
        return {"outcomes": [asdict(outcome) for outcome in self.outcomes]}
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_result.py -q`
Expected: PASS (13 tests).

- [x] **Step 5: Lint and commit**

```bash
uv run ruff check src/sqlproof/mutation/result.py tests/unit/test_mutation_result.py
git add src/sqlproof/mutation/result.py tests/unit/test_mutation_result.py
git commit -m "feat(mutation): outcome mapping and MutationResult.assert_no_survivors"
```

---

### Task 6: Local runner orchestration (unit-tested via fake)

**Files:**
- Create: `src/sqlproof/mutation/runner.py`
- Test: `tests/unit/test_mutation_runner.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/unit/test_mutation_runner.py
from __future__ import annotations

from sqlproof.mutation.apply import PreparedMutant
from sqlproof.mutation.model import Mutant, Replace
from sqlproof.mutation.runner import LocalMutationRunner


def _prepared(mutant_id: str, *, expect_survives: bool = False) -> PreparedMutant:
    mutant = Mutant(
        target_kind="function",
        target_name="total_usage",
        ops=(Replace("a", "b"),),
        expect_survives=expect_survives,
        reason="dead branch" if expect_survives else None,
    )
    return PreparedMutant(mutant=mutant, mutant_id=mutant_id, ddl="CREATE OR REPLACE ...")


class FakeRunner(LocalMutationRunner):
    """Overrides every side-effecting method; records the call sequence."""

    def __init__(self, exit_codes: dict[str, int], **kwargs: object) -> None:
        super().__init__(
            database_url="postgresql://localhost/base",
            pytest_args=["tests/test_billing.py"],
            **kwargs,  # type: ignore[arg-type]
        )
        self.exit_codes = exit_codes
        self.calls: list[tuple[str, str]] = []

    def _create_clone(self, clone_name: str) -> str:
        self.calls.append(("create", clone_name))
        return f"postgresql://localhost/{clone_name}"

    def _apply_ddl(self, clone_dsn: str, ddl: str) -> None:
        self.calls.append(("apply", clone_dsn))

    def _run_pytest(self, clone_dsn: str) -> tuple[int, str]:
        self.calls.append(("pytest", clone_dsn))
        clone_name = clone_dsn.rsplit("/", 1)[1]
        mutant_id = clone_name.removeprefix("sqlproof_mutant_")
        return self.exit_codes[mutant_id], "output tail"

    def _drop_clone(self, clone_name: str) -> None:
        self.calls.append(("drop", clone_name))


def test_runner_maps_exit_codes_to_statuses() -> None:
    runner = FakeRunner({"m1": 1, "m2": 0})
    result = runner.run([_prepared("m1"), _prepared("m2")])
    assert [o.status for o in result.outcomes] == ["killed", "survived"]
    assert result.outcomes[0].mutant_id == "m1"


def test_runner_respects_expect_survives() -> None:
    runner = FakeRunner({"m1": 0})
    result = runner.run([_prepared("m1", expect_survives=True)])
    assert result.outcomes[0].status == "expected_survivor"


def test_clone_is_dropped_even_when_pytest_raises() -> None:
    class ExplodingRunner(FakeRunner):
        def _run_pytest(self, clone_dsn: str) -> tuple[int, str]:
            raise OSError("pytest binary vanished")

    runner = ExplodingRunner({"m1": 0})
    result = runner.run([_prepared("m1")])
    assert result.outcomes[0].status == "error"
    assert "pytest binary vanished" in (result.outcomes[0].detail or "")
    assert ("drop", "sqlproof_mutant_m1") in runner.calls


def test_runner_runs_clone_apply_pytest_drop_in_order() -> None:
    runner = FakeRunner({"m1": 1})
    runner.run([_prepared("m1")])
    kinds = [kind for kind, _ in runner.calls]
    assert kinds == ["create", "apply", "pytest", "drop"]


def test_parallel_runner_preserves_outcome_order() -> None:
    runner = FakeRunner({"m1": 1, "m2": 0, "m3": 1}, max_workers=3)
    result = runner.run([_prepared("m1"), _prepared("m2"), _prepared("m3")])
    assert [o.mutant_id for o in result.outcomes] == ["m1", "m2", "m3"]
    assert [o.status for o in result.outcomes] == ["killed", "survived", "killed"]


def test_pytest_command_includes_seed_flag_when_set() -> None:
    runner = FakeRunner({}, hypothesis_seed=42)
    command = runner._pytest_command()
    assert "--hypothesis-seed=42" in command
    assert "tests/test_billing.py" in command


def test_pytest_command_omits_seed_flag_by_default() -> None:
    runner = FakeRunner({})
    assert not any(arg.startswith("--hypothesis-seed") for arg in runner._pytest_command())
```

- [x] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_mutation_runner.py -q`
Expected: FAIL — `ModuleNotFoundError` for `sqlproof.mutation.runner`.

- [x] **Step 3: Implement**

Create `src/sqlproof/mutation/runner.py`:

```python
from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
from psycopg import conninfo, sql

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.apply import PreparedMutant, prepare_mutants
from sqlproof.mutation.model import MutationSet
from sqlproof.mutation.result import MutantOutcome, MutationResult, outcome_for_exit_code

_OUTPUT_TAIL = 2000


class LocalMutationRunner:
    """Clone-per-mutant local execution.

    Each mutant gets a fresh database created from the template (the
    `database_url` database), the mutated DDL applied, and one pytest
    subprocess run with `env_var` pointing at the clone. The clone is
    dropped afterwards — there is no restore path by design.

    NOTE: `CREATE DATABASE ... TEMPLATE` requires the template database
    to have no other connections. Close dev sessions against it first.
    Clone creation is serialized behind a lock for the same reason.
    """

    def __init__(
        self,
        *,
        database_url: str,
        pytest_args: Sequence[str],
        env_var: str = "SQLPROOF_TEST_DATABASE_URL",
        maintenance_db: str = "postgres",
        hypothesis_seed: int | None = None,
        max_workers: int = 1,
    ) -> None:
        self.database_url = database_url
        self.pytest_args = list(pytest_args)
        self.env_var = env_var
        self.maintenance_db = maintenance_db
        self.hypothesis_seed = hypothesis_seed
        self.max_workers = max(1, max_workers)
        self._clone_lock = threading.Lock()

    # -- orchestration ------------------------------------------------

    def run(self, prepared: Sequence[PreparedMutant]) -> MutationResult:
        if self.max_workers == 1:
            outcomes = [self._run_one(p) for p in prepared]
        else:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                outcomes = list(pool.map(self._run_one, prepared))
        return MutationResult(outcomes=tuple(outcomes))

    def _run_one(self, prepared: PreparedMutant) -> MutantOutcome:
        clone_name = f"sqlproof_mutant_{prepared.mutant_id}"
        try:
            clone_dsn = self._create_clone(clone_name)
            try:
                self._apply_ddl(clone_dsn, prepared.ddl)
                exit_code, output = self._run_pytest(clone_dsn)
            finally:
                self._drop_clone(clone_name)
        except Exception as exc:
            return MutantOutcome(
                mutant_id=prepared.mutant_id,
                target=prepared.mutant.target_name,
                description=prepared.mutant.describe(),
                status="error",
                pytest_exit_code=None,
                hypothesis_seed=self.hypothesis_seed,
                detail=f"{type(exc).__name__}: {exc}",
            )
        return outcome_for_exit_code(
            mutant_id=prepared.mutant_id,
            target=prepared.mutant.target_name,
            description=prepared.mutant.describe(),
            expect_survives=prepared.mutant.expect_survives,
            exit_code=exit_code,
            hypothesis_seed=self.hypothesis_seed,
            detail=None if exit_code == 1 else output[-_OUTPUT_TAIL:],
        )

    # -- side-effecting seams (overridden in unit tests) ---------------

    def _create_clone(self, clone_name: str) -> str:
        template = self._dbname(self.database_url)
        with self._clone_lock, psycopg.connect(
            self._dsn_for(self.maintenance_db), autocommit=True
        ) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(clone_name))
            )
            connection.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                    sql.Identifier(clone_name), sql.Identifier(template)
                )
            )
        return self._dsn_for(clone_name)

    def _drop_clone(self, clone_name: str) -> None:
        with self._clone_lock, psycopg.connect(
            self._dsn_for(self.maintenance_db), autocommit=True
        ) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(clone_name))
            )

    def _apply_ddl(self, clone_dsn: str, ddl: str) -> None:
        with psycopg.connect(clone_dsn, autocommit=True) as connection:
            connection.execute(ddl)

    def _run_pytest(self, clone_dsn: str) -> tuple[int, str]:
        env = {**os.environ, self.env_var: clone_dsn}
        process = subprocess.run(
            self._pytest_command(),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        return process.returncode, process.stdout + process.stderr

    # -- helpers --------------------------------------------------------

    def _pytest_command(self) -> list[str]:
        command = [sys.executable, "-m", "pytest", *self.pytest_args]
        if self.hypothesis_seed is not None:
            command.append(f"--hypothesis-seed={self.hypothesis_seed}")
        return command

    def _dsn_for(self, dbname: str) -> str:
        parts = conninfo.conninfo_to_dict(self.database_url)
        parts["dbname"] = dbname
        return conninfo.make_conninfo(**parts)

    def _dbname(self, dsn: str) -> str:
        dbname = conninfo.conninfo_to_dict(dsn).get("dbname")
        if not dbname:
            msg = f"database_url has no dbname: {dsn!r}"
            raise SqlProofMutationError(msg)
        return str(dbname)


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
) -> MutationResult:
    """Prepare every mutant (all authoring errors raise here, before any
    database work), then run each against a fresh clone of `database_url`.

    `database_url` is the template database: schema applied, no
    connections open. `pytest_args` selects the suite; the subprocess
    sees `env_var` pointing at the per-mutant clone.
    """
    schema_sql = Path(schema_file).read_text(encoding="utf-8")
    prepared = prepare_mutants(mutations, schema_sql)
    runner = LocalMutationRunner(
        database_url=database_url,
        pytest_args=pytest_args,
        env_var=env_var,
        maintenance_db=maintenance_db,
        hypothesis_seed=hypothesis_seed,
        max_workers=max_workers,
    )
    return runner.run(prepared)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_runner.py -q`
Expected: PASS (7 tests).

- [x] **Step 5: Lint and commit**

```bash
uv run ruff check src/sqlproof/mutation/runner.py tests/unit/test_mutation_runner.py
git add src/sqlproof/mutation/runner.py tests/unit/test_mutation_runner.py
git commit -m "feat(mutation): clone-per-mutant local runner and run_mutation_tests"
```

---

### Task 7: Live integration test (gated on SQLPROOF_TEST_DATABASE_URL)

**Files:**
- Create: `tests/integration/test_mutation_live.py`

- [x] **Step 1: Write the integration test**

```python
# tests/integration/test_mutation_live.py
"""End-to-end mutation run against live Postgres.

Builds a throwaway template database (table + LANGUAGE sql function),
writes a minimal pytest suite to tmp_path, and runs two mutants:

  - WHERE-clause inversion -> the suite recomputes the sum in Python,
    so this mutant MUST be killed.
  - COALESCE fallback 0 -> 1 -> the suite never queries a user with
    zero rows, so this mutant MUST survive (the classic untested
    empty-group case).

Skips if SQLPROOF_TEST_DATABASE_URL is unset. Requires CREATEDB rights
on the target server (the CI supabase/postgres service has them).
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.model import MutationSet, Replace
from sqlproof.mutation.runner import run_mutation_tests

SCHEMA_SQL = """
CREATE TABLE usage_events (
    id serial PRIMARY KEY,
    user_id integer NOT NULL,
    amount integer NOT NULL CHECK (amount >= 0)
);

CREATE FUNCTION total_usage(p_user integer) RETURNS bigint
LANGUAGE sql STABLE
AS $$
    SELECT COALESCE(SUM(amount), 0) FROM usage_events WHERE user_id = p_user
$$;
"""

INNER_TEST = """
    from __future__ import annotations

    import os

    import psycopg


    def test_total_usage_matches_python_sum() -> None:
        dsn = os.environ["SQLPROOF_MUTATION_TEST_DSN"]
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute("DELETE FROM usage_events")
            rows = [(1, 10), (1, 32), (2, 5)]
            for user_id, amount in rows:
                connection.execute(
                    "INSERT INTO usage_events (user_id, amount) VALUES (%s, %s)",
                    (user_id, amount),
                )
            cursor = connection.execute("SELECT total_usage(1)")
            assert cursor.fetchone()[0] == 42
"""


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_mutation_run_kills_and_survives(tmp_path: Path) -> None:
    base_dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    template_name = f"sqlproof_mut_tmpl_{uuid4().hex[:12]}"

    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(template_name))
        )
    try:
        parts = psycopg.conninfo.conninfo_to_dict(base_dsn)
        parts["dbname"] = template_name
        template_dsn = psycopg.conninfo.make_conninfo(**parts)
        with psycopg.connect(template_dsn, autocommit=True) as connection:
            connection.execute(SCHEMA_SQL)
        # The template must have zero connections during cloning -> the
        # context managers above are closed before run_mutation_tests.

        schema_file = tmp_path / "schema.sql"
        schema_file.write_text(SCHEMA_SQL, encoding="utf-8")
        test_file = tmp_path / "test_inner_billing.py"
        test_file.write_text(textwrap.dedent(INNER_TEST), encoding="utf-8")

        mutations = MutationSet.for_function(
            "total_usage",
            [
                Replace("user_id = p_user", "user_id <> p_user"),
                Replace("COALESCE(SUM(amount), 0)", "COALESCE(SUM(amount), 1)"),
            ],
        )
        result = run_mutation_tests(
            mutations,
            schema_file=schema_file,
            database_url=template_dsn,
            pytest_args=[str(test_file), "-q", "-p", "no:cacheprovider"],
            env_var="SQLPROOF_MUTATION_TEST_DSN",
        )

        statuses = {o.description: o.status for o in result.outcomes}
        assert statuses[
            "total_usage: replace 'user_id = p_user' -> 'user_id <> p_user'"
        ] == "killed"
        assert statuses[
            "total_usage: replace 'COALESCE(SUM(amount), 0)' -> 'COALESCE(SUM(amount), 1)'"
        ] == "survived"

        with pytest.raises(SqlProofMutationError, match="COALESCE"):
            result.assert_no_survivors()

        # No clone databases left behind.
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            cursor = admin.execute(
                "SELECT datname FROM pg_database WHERE datname LIKE 'sqlproof_mutant_%'"
            )
            assert cursor.fetchall() == []
    finally:
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {}").format(
                    sql.Identifier(template_name)
                )
            )
```

Note `maintenance_db` defaults to `"postgres"` — if the CI service exposes a different always-on database, pass `maintenance_db=` accordingly. If `CREATE DATABASE ... TEMPLATE` fails with "source database is being accessed by other users", the leak is a connection to the template — every psycopg context manager touching it must be closed before `run_mutation_tests` (the code above already does this; preserve that property when editing).

- [x] **Step 2: Run the unit suite (integration skips locally without a DB)**

Run: `uv run pytest tests/integration/test_mutation_live.py -q`
Expected: 1 skipped (locally) — or PASS if `SQLPROOF_TEST_DATABASE_URL` is exported. If a local Postgres is available, export it and verify the test actually passes before committing:

```bash
SQLPROOF_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
  uv run pytest tests/integration/test_mutation_live.py -q
```

- [x] **Step 3: Commit**

```bash
uv run ruff check tests/integration/test_mutation_live.py
git add tests/integration/test_mutation_live.py
git commit -m "test(mutation): live end-to-end kill/survive integration test"
```

---

### Task 8: Public exports

**Files:**
- Modify: `src/sqlproof/__init__.py`
- Test: `tests/unit/test_mutation_model.py` (append)

- [x] **Step 1: Write the failing test**

Append to `tests/unit/test_mutation_model.py`:

```python
def test_public_exports() -> None:
    import sqlproof

    assert sqlproof.MutationSet is MutationSet
    assert sqlproof.Replace is Replace
    assert sqlproof.Drop is Drop
    from sqlproof.mutation.result import MutationResult
    from sqlproof.mutation.runner import run_mutation_tests

    assert sqlproof.run_mutation_tests is run_mutation_tests
    assert sqlproof.MutationResult is MutationResult
```

- [x] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_mutation_model.py::test_public_exports -q`
Expected: FAIL — `AttributeError: MutationSet`.

- [x] **Step 3: Implement**

In `src/sqlproof/__init__.py`, extend the existing lazy-import pattern exactly as the other exports do:

- Add to the `TYPE_CHECKING` block:

```python
    from sqlproof.mutation.model import Drop, MutationSet, Replace
    from sqlproof.mutation.result import MutationResult
    from sqlproof.mutation.runner import run_mutation_tests
```

- Add to `__all__` (keep it sorted): `"Drop"`, `"MutationResult"`, `"MutationSet"`, `"Replace"`, `"run_mutation_tests"`.

- Add to `__getattr__` before the final `raise AttributeError(name)`:

```python
    if name in {"MutationSet", "Replace", "Drop"}:
        from sqlproof.mutation import model

        return getattr(model, name)
    if name == "MutationResult":
        from sqlproof.mutation.result import MutationResult

        return MutationResult
    if name == "run_mutation_tests":
        from sqlproof.mutation.runner import run_mutation_tests

        return run_mutation_tests
```

- [x] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_mutation_model.py -q`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
uv run ruff check src/sqlproof/__init__.py tests/unit/test_mutation_model.py
git add src/sqlproof/__init__.py tests/unit/test_mutation_model.py
git commit -m "feat(mutation): export MutationSet, Replace, Drop, run_mutation_tests"
```

---

### Task 9: Full verification

- [x] **Step 1: Run the full CI-parity suite**

```bash
uv run pytest -q
uv run ruff check src/ tests/
uv run pyright
uv run mypy src/sqlproof/
```

Expected: pytest ≥ 283 + ~40 new passing (26+ skipped without DB); zero ruff/pyright/mypy findings. Fix anything that surfaces — pglast's lack of type stubs is the likely pyright/mypy friction; mirror the `Any` + targeted-ignore style already used in `schema/parse_sql.py`.

- [x] **Step 2: Run the integration test against a live database if available**

```bash
SQLPROOF_TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
  uv run pytest tests/integration/test_mutation_live.py -q
```

Expected: PASS. If no local Postgres exists, state that explicitly in the final report — do not claim live verification.

- [x] **Step 3: Commit any fixes**

```bash
git add -A
git commit -m "fix(mutation): typing and lint cleanup for mutation package"
```

(Skip the commit if Step 1 surfaced nothing.)

---

## Out of scope for this plan (tracked in the design doc / follow-ups)

- `MutationSet.for_policy` (RLS policy mutation) — next plan.
- Capped-Hypothesis-profile defaults per mutant run.
- `MutationRunner` protocol extraction + cloud runner.
- Static AST mutation catalog; LLM-proposed mutants; score history/reporting.
- Docs site page and README section — write after the API survives v1 usage.
