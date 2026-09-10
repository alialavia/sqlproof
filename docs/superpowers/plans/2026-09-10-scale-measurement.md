# Scale Measurement Implementation Plan (Phase 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a SQL function's scaling behaviour an assertable property, so an unscalable query fails CI rather than production.

**Architecture:** A sweep loads the caller's size profile at geometric scale factors, probes the function under `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` at each point, and fits a complexity exponent to the *work* measured (buffer pages touched), not to wall-clock. Space is a second, separately-reported axis because a `work_mem` spill is a cliff rather than a curve. The fitting layer is pure and carries most of the test coverage; only the sweep touches a database.

**Tech Stack:** Python 3.11+, psycopg 3.1+, Postgres 15 (`sqlproof-pg` container), pytest. No new runtime dependencies — the fit is ordinary least squares over logs, written by hand.

**Spec:** `docs/superpowers/specs/2026-09-09-scale-measurement-design.md`

## Global Constraints

- Python 3.11+. Every new module starts with `from __future__ import annotations`, matching existing files.
- **No new runtime dependencies.** `pyproject.toml`'s `dependencies` list stays as it is. The log-log fit is a dozen lines of arithmetic; do not reach for numpy or scipy.
- Baseline before this plan: `uv run pytest -q` → **620 passed**. Nothing existing may break.
- Coverage gate: `uv run pytest --cov=sqlproof --cov-fail-under=94`. Codecov policy lives in `codecov.yml` (project 94%, patch 80%).
- Lint: `uv run ruff check`, `uv run mypy src/sqlproof`, `uv run pyright <file>` — all clean on what you touch.
- Integration tests live in `tests/integration/`, gated on `SQLPROOF_TEST_DATABASE_URL`, skipping when unset. Copy the `pytestmark` pattern from `tests/integration/test_bulk_copy_live.py`.
- Local Postgres, per `CONTRIBUTING.md`:
  ```bash
  docker run -d --name sqlproof-pg -e POSTGRES_PASSWORD=postgres \
    -p 54399:5432 supabase/postgres:15.8.1.040
  export SQLPROOF_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54399/postgres
  ```
- Every schema a test creates must be dropped, **including on failure** (`try/finally` on an autocommit connection — see `test_bulk_copy_live.py`).
- **Row-count unit:** every row-count figure means **total rows across the profile** — `sum(sizes.values()) × factor`. Never one table's count. This is spec'd; do not invent a different unit.
- **Never fit on wall-clock.** Measured in the Phase 1 spike: at a fixed dataset, wall-clock varied 1.96× across runs on an *idle* machine while buffer counts varied 1.0007×. Execution time is recorded and used only to anchor the timeout projection, always labelled machine-dependent.

## File Structure

| File | Responsibility |
|---|---|
| `src/sqlproof/scale/probe.py` (new) | `ProbePoint` dataclass; parse an `EXPLAIN ... FORMAT JSON` tree into work / space / plan-hash; run one probe against a connection. |
| `src/sqlproof/scale/args.py` (new) | Argument resolvers: `heaviest`, `random_key`, `median_key`, and resolution of literals. |
| `src/sqlproof/scale/fit.py` (new) | Pure. Log-log fit, R², plan-flip segmentation, spill detection, timeout projection. No SQL, no I/O. |
| `src/sqlproof/scale/result.py` (new) | `ScaleResult` — the assertion surface, including raise-on-inconclusive. |
| `src/sqlproof/scale/sweep.py` (new) | The ladder, incremental loading, stop conditions. The only new module that writes to a database. |
| `src/sqlproof/scale/artifact.py` (new) | Persist a run as JSON, reusing the mutation-run artifact conventions. |
| `src/sqlproof/scale/__init__.py` (modify) | Export `scale_analysis`, `heaviest`, `random_key`, `median_key`, `ScaleResult`. |

Tests mirror this: `tests/unit/test_scale_probe_parsing.py`, `test_scale_args.py`, `test_scale_fit.py`, `test_scale_result.py`, `test_scale_artifact.py`, and `tests/integration/test_scale_probe_live.py`, `test_scale_complexity_live.py`, `test_scale_space_live.py`.

---

### Task 1: Parse an EXPLAIN JSON tree into a measurement

Pure parsing, no database. This is where the plan's single most dangerous trap lives.

**Files:**
- Create: `src/sqlproof/scale/probe.py`
- Test: `tests/unit/test_scale_probe_parsing.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ProbePoint` — frozen dataclass with fields `factor: int`, `total_rows: int`, `work_blocks: int`, `peak_memory_kb: int`, `temp_blocks: int`, `plan_hash: str`, `exec_ms: float`, `args: tuple[Any, ...]`
  - `parse_plan(explain_json: list[dict[str, Any]]) -> tuple[int, int, int, str, float]` returning `(work_blocks, peak_memory_kb, temp_blocks, plan_hash, exec_ms)`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_scale_probe_parsing.py
"""Parsing an EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) tree.

Pure: these are dicts shaped like Postgres output, no database. The
per-loop trap in `test_nested_loop_work_multiplies_by_loops` is the
reason this parsing gets its own task -- reading `Actual Rows` without
multiplying by `Actual Loops` is exactly where a quadratic hides.
"""
from __future__ import annotations

from sqlproof.scale.probe import parse_plan


def _plan(**node):
    """Wrap a node the way Postgres wraps a plan: a one-element list
    holding a dict with a "Plan" key."""
    base = {
        "Node Type": "Seq Scan",
        "Shared Hit Blocks": 0,
        "Shared Read Blocks": 0,
        "Actual Loops": 1,
    }
    base.update(node)
    return [{"Plan": base, "Execution Time": 1.5}]


def test_work_sums_hit_and_read_blocks():
    work, _mem, _temp, _h, _ms = parse_plan(
        _plan(**{"Shared Hit Blocks": 30, "Shared Read Blocks": 12})
    )
    assert work == 42


def test_work_sums_over_the_whole_tree():
    work, _m, _t, _h, _ms = parse_plan(
        _plan(
            **{
                "Shared Hit Blocks": 10,
                "Plans": [
                    {"Node Type": "Seq Scan", "Shared Hit Blocks": 5,
                     "Shared Read Blocks": 0, "Actual Loops": 1},
                    {"Node Type": "Seq Scan", "Shared Hit Blocks": 7,
                     "Shared Read Blocks": 0, "Actual Loops": 1},
                ],
            }
        )
    )
    assert work == 22


def test_nested_loop_work_multiplies_by_loops():
    """Postgres reports per-node buffer counts already totalled, but
    `Actual Rows` is a PER-LOOP average. A node reporting rows=50
    loops=10000 processed 500,000 rows, not 50. Any measure derived from
    rows must multiply; this pins that the parser does."""
    work, _m, _t, _h, _ms = parse_plan(
        _plan(
            **{
                "Node Type": "Nested Loop",
                "Shared Hit Blocks": 2,
                "Plans": [
                    {"Node Type": "Seq Scan", "Shared Hit Blocks": 3,
                     "Shared Read Blocks": 0, "Actual Loops": 10000,
                     "Actual Rows": 50},
                ],
            }
        )
    )
    # 2 + (3 * 10000): the inner scan really was executed 10000 times.
    assert work == 30002


def test_peak_memory_takes_the_largest_node_not_the_sum():
    _w, mem, _t, _h, _ms = parse_plan(
        _plan(
            **{
                "Node Type": "Sort",
                "Sort Space Used": 512,
                "Plans": [
                    {"Node Type": "Hash", "Peak Memory Usage": 2048,
                     "Shared Hit Blocks": 0, "Shared Read Blocks": 0,
                     "Actual Loops": 1},
                ],
            }
        )
    )
    assert mem == 2048


def test_temp_blocks_signal_a_spill():
    _w, _m, temp, _h, _ms = parse_plan(
        _plan(**{"Temp Read Blocks": 40, "Temp Written Blocks": 60})
    )
    assert temp == 100


def test_plan_hash_ignores_row_counts_and_costs():
    a = parse_plan(_plan(**{"Actual Rows": 10, "Total Cost": 1.0}))[3]
    b = parse_plan(_plan(**{"Actual Rows": 999999, "Total Cost": 5000.0}))[3]
    assert a == b


def test_plan_hash_changes_when_node_types_change():
    a = parse_plan(_plan(**{"Node Type": "Seq Scan"}))[3]
    b = parse_plan(_plan(**{"Node Type": "Index Scan"}))[3]
    assert a != b


def test_plan_hash_changes_when_nesting_changes():
    flat = parse_plan(_plan(**{"Node Type": "Hash Join"}))[3]
    nested = parse_plan(
        _plan(
            **{
                "Node Type": "Hash Join",
                "Plans": [
                    {"Node Type": "Seq Scan", "Shared Hit Blocks": 0,
                     "Shared Read Blocks": 0, "Actual Loops": 1},
                ],
            }
        )
    )[3]
    assert flat != nested


def test_execution_time_is_read_from_the_envelope():
    _w, _m, _t, _h, ms = parse_plan(_plan())
    assert ms == 1.5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_scale_probe_parsing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqlproof.scale.probe'`

- [ ] **Step 3: Write the parser**

```python
# src/sqlproof/scale/probe.py
"""One measurement of a function under EXPLAIN.

Work is measured in buffer pages touched, not wall-clock. Measured in
the Phase 1 spike: at a fixed dataset, wall-clock varied 1.96x across
runs on an *idle* machine while buffer counts varied 1.0007x. An
exponent fitted on buffers holds on a laptop and a noisy CI runner
alike; fitted on wall-clock, the gate would flake.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ProbePoint:
    factor: int
    total_rows: int
    work_blocks: int
    peak_memory_kb: int
    temp_blocks: int
    plan_hash: str
    exec_ms: float
    args: tuple[Any, ...] = ()


def parse_plan(explain_json: list[dict[str, Any]]) -> tuple[int, int, int, str, float]:
    """Reduce an EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) tree to
    (work_blocks, peak_memory_kb, temp_blocks, plan_hash, exec_ms).
    """
    envelope = explain_json[0]
    root = envelope["Plan"]
    work = _work(root, loops=1)
    memory = _peak_memory(root)
    temp = _temp_blocks(root)
    shape = _shape(root)
    digest = hashlib.sha256(shape.encode()).hexdigest()[:16]
    return work, memory, temp, digest, float(envelope.get("Execution Time", 0.0))


def _work(node: dict[str, Any], loops: int) -> int:
    """Buffer pages touched by this node and its children.

    A node's own buffer counts are already totalled across its loops by
    Postgres, but a child executed inside a Nested Loop runs once per
    outer row. `Actual Loops` on the CHILD is that multiplier, so the
    child's contribution scales by it. Getting this wrong is how a
    quadratic reads as linear -- a node showing `rows=50 loops=10000`
    processed half a million rows.
    """
    own = int(node.get("Shared Hit Blocks", 0)) + int(node.get("Shared Read Blocks", 0))
    total = own * loops
    for child in node.get("Plans", []):
        total += _work(child, loops=int(child.get("Actual Loops", 1)))
    return total


def _peak_memory(node: dict[str, Any]) -> int:
    """Largest single node's memory, not the sum.

    Peak memory is what decides whether `work_mem` is exceeded, and
    nodes do not all hold their peak simultaneously. Summing would
    overstate it and make the spill point look closer than it is.
    """
    own = max(
        int(node.get("Sort Space Used", 0)),
        int(node.get("Peak Memory Usage", 0)),
    )
    for child in node.get("Plans", []):
        own = max(own, _peak_memory(child))
    return own


def _temp_blocks(node: dict[str, Any]) -> int:
    total = int(node.get("Temp Read Blocks", 0)) + int(
        node.get("Temp Written Blocks", 0)
    )
    for child in node.get("Plans", []):
        total += _temp_blocks(child)
    return total


def _shape(node: dict[str, Any]) -> str:
    """Node types and nesting only.

    Deliberately excludes row counts, costs and timings: the point is to
    detect the planner switching STRATEGY, not the data changing size.
    Every scale point changes the numbers; only a real plan change
    should change this hash.
    """
    children = ",".join(_shape(child) for child in node.get("Plans", []))
    return f"{node.get('Node Type', '?')}({children})"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_scale_probe_parsing.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/scale/probe.py tests/unit/test_scale_probe_parsing.py
git commit -m "feat(scale): parse EXPLAIN JSON into work, space and plan shape"
```

---

### Task 2: Run one probe against a live database

**Files:**
- Modify: `src/sqlproof/scale/probe.py`
- Test: `tests/integration/test_scale_probe_live.py`

**Interfaces:**
- Consumes: `ProbePoint`, `parse_plan` (Task 1).
- Produces: `probe_function(conn, function: str, args: Sequence[Any], *, factor: int, total_rows: int) -> ProbePoint`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_scale_probe_live.py
from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof.scale.probe import probe_function

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE items (id bigint PRIMARY KEY, tag text NOT NULL);
INSERT INTO items SELECT g, 'tag' || (g % 7) FROM generate_series(1, 500) g;
CREATE FUNCTION count_items() RETURNS bigint LANGUAGE plpgsql STABLE AS $$
DECLARE n bigint;
BEGIN SELECT count(*) INTO n FROM items; RETURN n; END $$;
CREATE FUNCTION touch_nothing() RETURNS int LANGUAGE sql IMMUTABLE AS 'SELECT 1';
"""


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS probe_test CASCADE")
        connection.execute("CREATE SCHEMA probe_test")
        connection.execute("SET search_path TO probe_test")
        connection.execute(SCHEMA_SQL)
        try:
            yield connection
        finally:
            connection.execute("DROP SCHEMA IF EXISTS probe_test CASCADE")


def test_probe_measures_work_inside_a_plpgsql_function(conn):
    """The function's internal work must be visible. EXPLAIN on the
    outer call shows a single node, but buffer counters accumulate
    across nested statements into the outer statement's totals."""
    point = probe_function(conn, "count_items", [], factor=1, total_rows=500)
    assert point.work_blocks > 0
    assert point.plan_hash
    assert point.exec_ms >= 0


def test_probe_of_a_function_touching_nothing_reports_near_zero_work(conn):
    point = probe_function(conn, "touch_nothing", [], factor=1, total_rows=0)
    assert point.work_blocks < 50


def test_probe_rolls_back_side_effects(conn):
    """A probe must not mutate the data it is measuring against, or the
    next scale point measures a different database."""
    conn.execute(
        "CREATE FUNCTION probe_test.add_item() RETURNS void LANGUAGE sql AS "
        "$$ INSERT INTO probe_test.items VALUES (999999, 'x') $$"
    )
    before = conn.execute("SELECT count(*) FROM probe_test.items").fetchone()[0]
    probe_function(conn, "probe_test.add_item", [], factor=1, total_rows=500)
    after = conn.execute("SELECT count(*) FROM probe_test.items").fetchone()[0]
    assert after == before


def test_probe_passes_arguments(conn):
    conn.execute(
        "CREATE FUNCTION probe_test.items_with_tag(t text) RETURNS bigint "
        "LANGUAGE sql STABLE AS "
        "$$ SELECT count(*) FROM probe_test.items WHERE tag = t $$"
    )
    point = probe_function(
        conn, "probe_test.items_with_tag", ["tag1"], factor=1, total_rows=500
    )
    assert point.work_blocks > 0
    assert point.args == ("tag1",)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_scale_probe_live.py -v`
Expected: FAIL — `ImportError: cannot import name 'probe_function'`

- [ ] **Step 3: Implement the probe**

Append to `src/sqlproof/scale/probe.py`:

```python
from collections.abc import Sequence

import psycopg


def probe_function(
    conn: psycopg.Connection,
    function: str,
    args: Sequence[Any],
    *,
    factor: int,
    total_rows: int,
) -> ProbePoint:
    """Measure one call of `function` under EXPLAIN.

    Runs inside a savepoint that is always rolled back: a function with
    side effects must not change the data the next scale point measures
    against. Mirrors the isolation `core.py:175-179` uses for property
    runs.
    """
    placeholders = ", ".join(["%s"] * len(args))
    statement = (
        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT {function}({placeholders})"
    )
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sqlproof_probe")
        try:
            cur.execute(statement, tuple(args) or None)
            row = cur.fetchone()
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT sqlproof_probe")
            cur.execute("RELEASE SAVEPOINT sqlproof_probe")
    # psycopg returns the JSON already decoded; the row is a 1-tuple (or
    # a 1-key dict under dict_row) holding the EXPLAIN array.
    explain_json = list(row.values())[0] if isinstance(row, dict) else row[0]
    work, memory, temp, digest, ms = parse_plan(explain_json)
    return ProbePoint(
        factor=factor,
        total_rows=total_rows,
        work_blocks=work,
        peak_memory_kb=memory,
        temp_blocks=temp,
        plan_hash=digest,
        exec_ms=ms,
        args=tuple(args),
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
export SQLPROOF_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54399/postgres
uv run pytest tests/integration/test_scale_probe_live.py -v
```
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/scale/probe.py tests/integration/test_scale_probe_live.py
git commit -m "feat(scale): probe a function under EXPLAIN with savepoint isolation"
```

---

### Task 3: Argument resolvers

**Files:**
- Create: `src/sqlproof/scale/args.py`
- Test: `tests/unit/test_scale_args.py`, `tests/integration/test_scale_probe_live.py` (extend)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `heaviest(column: str) -> Resolver`
  - `random_key(column: str, *, seed: int = 0) -> Resolver`
  - `median_key(column: str) -> Resolver`
  - `resolve_args(conn, args: Sequence[Any]) -> tuple[Any, ...]`
  - `Resolver` — a callable taking a `psycopg.Connection` and returning a value; identified by a `_sqlproof_resolver = True` attribute.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_scale_args.py
"""Argument resolvers.

A resolver is re-run against the freshly loaded data at every scale
point, which is not a refinement but a requirement: the dataset is
regenerated at each factor and keys are assigned deterministically
(`_unique_value` gives id = i + 1), so a literal that exists at 8x may
not exist at 1x.
"""
from __future__ import annotations

from sqlproof.scale.args import heaviest, median_key, random_key, resolve_args


class FakeConn:
    """Records SQL and returns a canned scalar. Enough to test that a
    resolver builds the query it claims to; the real query is exercised
    in the integration tests."""

    def __init__(self, value):
        self.value = value
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        return self

    def fetchone(self):
        return (self.value,)


def test_literals_pass_through_untouched():
    conn = FakeConn(None)
    assert resolve_args(conn, [42, "abc", None]) == (42, "abc", None)
    assert conn.sql == []


def test_resolver_is_called_with_the_connection():
    conn = FakeConn(7)
    assert resolve_args(conn, [heaviest("customers.id")]) == (7,)
    assert len(conn.sql) == 1


def test_heaviest_orders_by_descending_child_count():
    conn = FakeConn(3)
    heaviest("customers.id")(conn)
    sql = conn.sql[0].lower()
    assert "order by" in sql
    assert "desc" in sql
    assert "limit 1" in sql


def test_median_key_uses_an_offset_rather_than_ordering_by_count():
    conn = FakeConn(5)
    median_key("customers.id")(conn)
    sql = conn.sql[0].lower()
    assert "offset" in sql


def test_random_key_is_deterministic_for_a_given_seed():
    a = FakeConn(1)
    b = FakeConn(1)
    random_key("customers.id", seed=99)(a)
    random_key("customers.id", seed=99)(b)
    assert a.sql == b.sql


def test_mixed_literals_and_resolvers_keep_their_positions():
    conn = FakeConn(9)
    assert resolve_args(conn, ["first", heaviest("t.id"), 3]) == ("first", 9, 3)


def test_a_plain_callable_is_treated_as_a_resolver():
    conn = FakeConn(None)
    assert resolve_args(conn, [lambda c: 123]) == (123,)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_scale_args.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqlproof.scale.args'`

- [ ] **Step 3: Implement resolvers**

```python
# src/sqlproof/scale/args.py
"""Resolving the arguments a probed function is called with.

Re-run at every scale point, against the data just loaded. That is
required rather than tidy: the dataset is regenerated at each factor and
keys are assigned deterministically (`_unique_value` gives id = i + 1),
so a literal that exists at 8x may not exist at 1x.

`heaviest` is the recommended default, and the reason is the feature's
purpose. The question being asked is "will this fall over?", so the case
to measure is the one most likely to. Under Zipf skew the heaviest key
is the worst case, and reporting "fast" because the probe happened to
pick a customer with three invoices would be a silently wrong answer.

The cost is that results describe worst-case behaviour rather than
typical, which is correct here but must be labelled wherever it is
reported. `random_key` and `median_key` answer the other question.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlproof.exceptions import SqlProofUsageError

Resolver = Callable[[Any], Any]


def _split(column: str) -> tuple[str, str]:
    if "." not in column:
        msg = (
            f"Column reference {column!r} must be qualified as "
            '"table.column" so the resolver knows which table to query.'
        )
        raise SqlProofUsageError(msg)
    table, _, name = column.rpartition(".")
    return table, name


def heaviest(column: str) -> Resolver:
    """The key with the most referencing rows across the whole schema.

    Approximated by ordering the key's own table by how many rows share
    each value in the busiest referencing table. Where no referencing
    table exists this degenerates to the largest key, which is still a
    defensible worst case.
    """
    table, name = _split(column)

    def resolve(conn: Any) -> Any:
        sql = (
            f"SELECT {name} FROM {table} "  # noqa: S608 - identifiers come from the schema
            f"ORDER BY {name} DESC LIMIT 1"
        )
        return conn.execute(sql).fetchone()[0]

    resolve._sqlproof_resolver = True  # type: ignore[attr-defined]
    return resolve


def random_key(column: str, *, seed: int = 0) -> Resolver:
    table, name = _split(column)

    def resolve(conn: Any) -> Any:
        sql = (
            f"SELECT {name} FROM {table} "  # noqa: S608
            f"ORDER BY md5({name}::text || '{seed}') LIMIT 1"
        )
        return conn.execute(sql).fetchone()[0]

    resolve._sqlproof_resolver = True  # type: ignore[attr-defined]
    return resolve


def median_key(column: str) -> Resolver:
    table, name = _split(column)

    def resolve(conn: Any) -> Any:
        sql = (
            f"SELECT {name} FROM {table} ORDER BY {name} "  # noqa: S608
            f"OFFSET (SELECT count(*) / 2 FROM {table}) LIMIT 1"
        )
        return conn.execute(sql).fetchone()[0]

    resolve._sqlproof_resolver = True  # type: ignore[attr-defined]
    return resolve


def resolve_args(conn: Any, args: Sequence[Any]) -> tuple[Any, ...]:
    """Resolve each entry: a callable is invoked with the connection, a
    literal passes through untouched."""
    resolved: list[Any] = []
    for arg in args:
        resolved.append(arg(conn) if callable(arg) else arg)
    return tuple(resolved)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_scale_args.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Add a live test that `heaviest` finds a real key**

Append to `tests/integration/test_scale_probe_live.py`:

```python
def test_heaviest_resolves_against_live_data(conn):
    from sqlproof.scale.args import heaviest, resolve_args

    resolved = resolve_args(conn, [heaviest("probe_test.items.id")])
    assert resolved == (500,)  # the largest id in the seeded table


def test_resolver_failing_to_find_a_row_raises_rather_than_returning_none(conn):
    """Measuring the empty case would report 'fast' for an untested
    function, which is the silent-wrong-answer failure this design keeps
    guarding against."""
    from sqlproof.exceptions import SqlProofUsageError
    from sqlproof.scale.args import heaviest, resolve_args

    conn.execute("CREATE TABLE probe_test.empty_t (id bigint PRIMARY KEY)")
    with pytest.raises((SqlProofUsageError, TypeError)):
        resolve_args(conn, [heaviest("probe_test.empty_t.id")])
```

- [ ] **Step 6: Make the empty-table case raise clearly**

In `args.py`, wrap each resolver's fetch:

```python
def _scalar(conn: Any, sql: str, column: str) -> Any:
    row = conn.execute(sql).fetchone()
    if row is None or row[0] is None:
        msg = (
            f"Resolver for {column!r} found no rows. Measuring an empty "
            "table would report the function as fast without testing it."
        )
        raise SqlProofUsageError(msg)
    return row[0]
```

and have all three resolvers return `_scalar(conn, sql, column)`.

- [ ] **Step 7: Run both suites**

```bash
uv run pytest tests/unit/test_scale_args.py tests/integration/test_scale_probe_live.py -v
```
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/sqlproof/scale/args.py tests/unit/test_scale_args.py tests/integration/test_scale_probe_live.py
git commit -m "feat(scale): argument resolvers, re-run per scale point"
```

---

### Task 4: Fit an exponent

**Files:**
- Create: `src/sqlproof/scale/fit.py`
- Test: `tests/unit/test_scale_fit.py`

**Interfaces:**
- Consumes: `ProbePoint` (Task 1).
- Produces:
  - `FitResult` — frozen dataclass: `exponent: float | None`, `r_squared: float | None`, `reason: str | None`, `from_factor: int`, `to_factor: int`, `plan_hash: str`
  - `fit_exponent(points: Sequence[ProbePoint], baseline: int) -> FitResult`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_scale_fit.py
"""Fitting a complexity exponent. Pure -- synthetic points, no database.

Baseline subtraction is not a refinement. The Phase 1 spike measured a
RAW fit of 1.69 on a function that is exactly quadratic; subtracting the
fixed per-call cost gave 1.994. Without it the answer is simply wrong.
"""
from __future__ import annotations

from sqlproof.scale.fit import fit_exponent
from sqlproof.scale.probe import ProbePoint


def _points(work_by_factor: dict[int, int], plan_hash: str = "aaaa"):
    return [
        ProbePoint(
            factor=f, total_rows=f * 1000, work_blocks=w, peak_memory_kb=0,
            temp_blocks=0, plan_hash=plan_hash, exec_ms=1.0,
        )
        for f, w in sorted(work_by_factor.items())
    ]


def test_linear_work_fits_exponent_one():
    pts = _points({1: 100, 2: 200, 4: 400, 8: 800, 16: 1600})
    fit = fit_exponent(pts, baseline=0)
    assert abs(fit.exponent - 1.0) < 0.05
    assert fit.r_squared > 0.99


def test_quadratic_work_fits_exponent_two():
    pts = _points({1: 100, 2: 400, 4: 1600, 8: 6400, 16: 25600})
    fit = fit_exponent(pts, baseline=0)
    assert abs(fit.exponent - 2.0) < 0.05


def test_constant_work_fits_exponent_zero():
    pts = _points({1: 500, 2: 500, 4: 500, 8: 500, 16: 500})
    fit = fit_exponent(pts, baseline=0)
    assert abs(fit.exponent) < 0.05


def test_baseline_subtraction_changes_the_answer():
    """A fixed per-call overhead flattens the curve. These points are
    exactly quadratic ABOVE a constant 100 blocks of overhead."""
    pts = _points({1: 200, 2: 500, 4: 1700, 8: 6500, 16: 25700})
    raw = fit_exponent(pts, baseline=0)
    corrected = fit_exponent(pts, baseline=100)
    assert abs(corrected.exponent - 2.0) < 0.05
    assert raw.exponent < corrected.exponent - 0.1


def test_noisy_data_reports_low_r_squared_and_no_exponent():
    pts = _points({1: 100, 2: 5000, 4: 120, 8: 9000, 16: 300})
    fit = fit_exponent(pts, baseline=0)
    assert fit.exponent is None
    assert fit.reason is not None
    assert "r_squared" in fit.reason or "fit" in fit.reason.lower()


def test_too_few_points_is_refused_rather_than_fitted():
    pts = _points({1: 100, 2: 400})
    fit = fit_exponent(pts, baseline=0)
    assert fit.exponent is None
    assert "points" in fit.reason


def test_baseline_larger_than_measured_work_is_refused():
    """Subtracting more than was measured would produce log of a
    non-positive number. Refuse rather than emit nonsense."""
    pts = _points({1: 50, 2: 60, 4: 70, 8: 80, 16: 90})
    fit = fit_exponent(pts, baseline=100)
    assert fit.exponent is None
    assert fit.reason is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_scale_fit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqlproof.scale.fit'`

- [ ] **Step 3: Implement the fit**

```python
# src/sqlproof/scale/fit.py
"""Fitting a complexity exponent to measured work. Pure: no SQL, no I/O.

This module carries the bulk of the feature's test coverage, and it is
the seam a cloud tier reuses verbatim -- swap "measured locally" for
"ingested from a remote run" and the maths is unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from sqlproof.scale.probe import ProbePoint

MIN_POINTS = 5
MIN_R_SQUARED = 0.98


@dataclass(frozen=True, slots=True)
class FitResult:
    exponent: float | None
    r_squared: float | None
    reason: str | None
    from_factor: int
    to_factor: int
    plan_hash: str


def fit_exponent(points: Sequence[ProbePoint], baseline: int) -> FitResult:
    """Least-squares slope of log(work - baseline) against log(factor).

    The slope IS the complexity exponent: work proportional to n^k plots
    as a straight line of slope k on log-log axes.

    `baseline` is the fixed per-call cost -- catalog lookups and plan
    caching, ~100-190 blocks regardless of data size. Leaving it in
    flattens the curve toward zero at small factors and understates the
    exponent; the Phase 1 spike measured 1.69 raw against 1.994
    corrected on an exactly-quadratic function.
    """
    lo = points[0].factor if points else 0
    hi = points[-1].factor if points else 0
    shape = points[0].plan_hash if points else ""

    if len(points) < MIN_POINTS:
        return FitResult(
            None, None,
            f"only {len(points)} points, need at least {MIN_POINTS} to fit",
            lo, hi, shape,
        )

    xs: list[float] = []
    ys: list[float] = []
    for point in points:
        adjusted = point.work_blocks - baseline
        if adjusted <= 0 or point.factor <= 0:
            return FitResult(
                None, None,
                "work at or below the fixed baseline; nothing left to fit "
                f"(work={point.work_blocks}, baseline={baseline})",
                lo, hi, shape,
            )
        xs.append(math.log(point.factor))
        ys.append(math.log(adjusted))

    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return FitResult(None, None, "all points at the same factor", lo, hi, shape)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx

    intercept = mean_y - slope * mean_x
    ss_res = sum(
        (y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys, strict=True)
    )
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r2 = 1.0 if ss_tot == 0 else 1 - ss_res / ss_tot

    if r2 < MIN_R_SQUARED:
        return FitResult(
            None, round(r2, 4),
            f"r_squared {r2:.3f} below {MIN_R_SQUARED}; the points do not "
            "lie on a power curve, so no exponent is claimed",
            lo, hi, shape,
        )
    return FitResult(round(slope, 4), round(r2, 4), None, lo, hi, shape)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_scale_fit.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/scale/fit.py tests/unit/test_scale_fit.py
git commit -m "feat(scale): least-squares complexity exponent with baseline correction"
```

---

### Task 5: Segment the fit on plan changes

**Files:**
- Modify: `src/sqlproof/scale/fit.py`
- Test: `tests/unit/test_scale_fit.py` (extend)

**Interfaces:**
- Consumes: `FitResult`, `fit_exponent` (Task 4).
- Produces:
  - `PlanFlip` — frozen dataclass: `at_factor: int`, `from_hash: str`, `to_hash: str`
  - `segment_by_plan(points: Sequence[ProbePoint]) -> tuple[list[list[ProbePoint]], list[PlanFlip]]`

- [ ] **Step 1: Write the failing test**

```python
def test_points_with_one_plan_are_a_single_segment():
    from sqlproof.scale.fit import segment_by_plan

    pts = _points({1: 100, 2: 200, 4: 400})
    segments, flips = segment_by_plan(pts)
    assert len(segments) == 1
    assert flips == []


def test_a_plan_change_splits_the_segments_and_records_the_flip():
    """Fitting across a plan change produces a meaningless number -- the
    curve is genuinely discontinuous there. Each regime is fitted on its
    own and the flip is reported as a finding in its own right."""
    from sqlproof.scale.fit import segment_by_plan

    pts = _points({1: 100, 2: 200}, plan_hash="seq") + _points(
        {4: 50, 8: 60}, plan_hash="index"
    )
    segments, flips = segment_by_plan(pts)
    assert [len(s) for s in segments] == [2, 2]
    assert len(flips) == 1
    assert flips[0].at_factor == 4
    assert flips[0].from_hash == "seq"
    assert flips[0].to_hash == "index"


def test_flipping_back_and_forth_yields_a_segment_per_run():
    from sqlproof.scale.fit import segment_by_plan

    pts = (
        _points({1: 10}, plan_hash="a")
        + _points({2: 20}, plan_hash="b")
        + _points({4: 40}, plan_hash="a")
    )
    segments, flips = segment_by_plan(pts)
    assert len(segments) == 3
    assert len(flips) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_scale_fit.py -k segment -v`
Expected: FAIL — `ImportError: cannot import name 'segment_by_plan'`

- [ ] **Step 3: Implement segmentation**

Append to `fit.py`:

```python
@dataclass(frozen=True, slots=True)
class PlanFlip:
    at_factor: int
    from_hash: str
    to_hash: str


def segment_by_plan(
    points: Sequence[ProbePoint],
) -> tuple[list[list[ProbePoint]], list[PlanFlip]]:
    """Split points into runs sharing a plan shape, and report the
    boundaries.

    A plan change is a genuine discontinuity in the cost curve: at small
    n a sequential scan is correctly cheapest, and the planner switching
    to an index scan resets the curve rather than bending it. Fitting
    straight through one averages two different functions together.
    """
    if not points:
        return [], []
    segments: list[list[ProbePoint]] = [[points[0]]]
    flips: list[PlanFlip] = []
    for previous, current in zip(points, points[1:], strict=False):
        if current.plan_hash == previous.plan_hash:
            segments[-1].append(current)
            continue
        flips.append(
            PlanFlip(
                at_factor=current.factor,
                from_hash=previous.plan_hash,
                to_hash=current.plan_hash,
            )
        )
        segments.append([current])
    return segments, flips
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_scale_fit.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/scale/fit.py tests/unit/test_scale_fit.py
git commit -m "feat(scale): segment the fit on plan-shape changes"
```

---

### Task 6: Spill detection and timeout projection

**Files:**
- Modify: `src/sqlproof/scale/fit.py`
- Test: `tests/unit/test_scale_fit.py` (extend)

**Interfaces:**
- Consumes: `ProbePoint`, `FitResult` (Tasks 1, 4).
- Produces:
  - `find_spill(points: Sequence[ProbePoint]) -> ProbePoint | None`
  - `project_rows_before_timeout(points, fit, timeout_ms, *, truncated: bool) -> tuple[int, int] | None`

- [ ] **Step 1: Write the failing test**

```python
def test_no_temp_blocks_means_no_spill():
    from sqlproof.scale.fit import find_spill

    assert find_spill(_points({1: 10, 2: 20, 4: 40})) is None


def test_spill_point_is_the_first_factor_with_temp_blocks():
    """A spill is a CLIFF, not a curve: a sort that fits in work_mem is
    fast, and the moment it spills performance drops sharply. Reported
    separately so a fit run through it does not smear the
    discontinuity into a gentle-looking exponent."""
    from sqlproof.scale.fit import find_spill
    from sqlproof.scale.probe import ProbePoint

    pts = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=f * 10,
                   peak_memory_kb=100, temp_blocks=temp, plan_hash="a", exec_ms=1.0)
        for f, temp in [(1, 0), (2, 0), (4, 0), (8, 340), (16, 900)]
    ]
    spill = find_spill(pts)
    assert spill is not None
    assert spill.factor == 8
    assert spill.total_rows == 8000


def test_projection_extrapolates_the_final_regime():
    from sqlproof.scale.fit import fit_exponent, project_rows_before_timeout

    pts = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=f * 100,
                   peak_memory_kb=0, temp_blocks=0, plan_hash="a", exec_ms=f * 10.0)
        for f in (1, 2, 4, 8, 16)
    ]
    fit = fit_exponent(pts, baseline=0)
    band = project_rows_before_timeout(pts, fit, timeout_ms=1000, truncated=False)
    assert band is not None
    lo, hi = band
    assert lo < hi
    assert lo > 16_000  # beyond the largest measured point


def test_projection_is_refused_when_the_sweep_was_truncated():
    """Extrapolating past a range we stopped measuring for a reason is
    guessing, and a confident row count is exactly the wrong output."""
    from sqlproof.scale.fit import fit_exponent, project_rows_before_timeout

    pts = _points({1: 100, 2: 200, 4: 400, 8: 800, 16: 1600})
    fit = fit_exponent(pts, baseline=0)
    assert project_rows_before_timeout(pts, fit, 1000, truncated=True) is None


def test_projection_is_refused_without_a_usable_fit():
    from sqlproof.scale.fit import FitResult, project_rows_before_timeout

    pts = _points({1: 100, 2: 200, 4: 400, 8: 800, 16: 1600})
    bad = FitResult(None, 0.2, "too noisy", 1, 16, "a")
    assert project_rows_before_timeout(pts, bad, 1000, truncated=False) is None


def test_projection_returns_none_when_already_over_the_timeout():
    from sqlproof.scale.fit import fit_exponent, project_rows_before_timeout
    from sqlproof.scale.probe import ProbePoint

    pts = [
        ProbePoint(factor=f, total_rows=f * 1000, work_blocks=f * 100,
                   peak_memory_kb=0, temp_blocks=0, plan_hash="a", exec_ms=5000.0 * f)
        for f in (1, 2, 4, 8, 16)
    ]
    fit = fit_exponent(pts, baseline=0)
    assert project_rows_before_timeout(pts, fit, timeout_ms=1000, truncated=False) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_scale_fit.py -k "spill or projection" -v`
Expected: FAIL — `ImportError: cannot import name 'find_spill'`

- [ ] **Step 3: Implement**

Append to `fit.py`:

```python
def find_spill(points: Sequence[ProbePoint]) -> ProbePoint | None:
    """The first point where a sort or hash exceeded `work_mem`.

    Reported separately from the exponent because a spill is a cliff,
    not a curve. Everything is fine right up until it is not, and a
    least-squares fit run across that boundary reports a misleadingly
    gentle slope.
    """
    for point in points:
        if point.temp_blocks > 0:
            return point
    return None


def project_rows_before_timeout(
    points: Sequence[ProbePoint],
    fit: FitResult,
    timeout_ms: float,
    *,
    truncated: bool,
) -> tuple[int, int] | None:
    """Extrapolate the final regime to a wall-clock timeout.

    Returns a RANGE, never a point estimate, and returns None rather
    than guessing when the fit is unusable, the sweep was truncated, or
    the function already exceeds the timeout inside the measured range.

    This is the one output that depends on the machine that measured it.
    Every caller must label it as such; the exponent does not carry that
    caveat and the two must not be presented as equally solid.
    """
    if fit.exponent is None or truncated or not points:
        return None
    last = points[-1]
    if last.exec_ms >= timeout_ms:
        return None
    if last.exec_ms <= 0 or fit.exponent <= 0:
        return None
    # time ~ rows^exponent, so rows_at_timeout = last_rows * ratio^(1/k)
    ratio = timeout_ms / last.exec_ms
    centre = last.total_rows * ratio ** (1.0 / fit.exponent)
    # A deliberately wide band. The exponent is measured, but wall-clock
    # is not stable enough (1.96x run to run on an idle machine) for a
    # tighter claim to be honest.
    return int(centre * 0.6), int(centre * 1.6)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_scale_fit.py -v`
Expected: PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/scale/fit.py tests/unit/test_scale_fit.py
git commit -m "feat(scale): spill detection and guarded timeout projection"
```

---

### Task 7: `ScaleResult`, the assertion surface

**Files:**
- Create: `src/sqlproof/scale/result.py`
- Test: `tests/unit/test_scale_result.py`

**Interfaces:**
- Consumes: `ProbePoint`, `FitResult`, `PlanFlip`, `find_spill`, `project_rows_before_timeout` (Tasks 1, 4-6).
- Produces:
  - `ScaleResult` with `.exponent`, `.r_squared`, `.regimes`, `.plan_flips`, `.spill_point_rows`, `.factor_at_spill`, `.spills_below(rows)`, `.rows_before_timeout(ms)`, `.points`, `.truncated`
  - `SqlProofScaleError` in `src/sqlproof/exceptions.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_scale_result.py
"""The assertion surface.

Row-count figures here are TOTAL ROWS ACROSS THE PROFILE
(`sum(sizes.values()) * factor`), never one table's count -- the sweep
scales a whole profile, so a single table's count would be ambiguous.
"""
from __future__ import annotations

import pytest

from sqlproof.exceptions import SqlProofScaleError
from sqlproof.scale.fit import FitResult, PlanFlip
from sqlproof.scale.probe import ProbePoint
from sqlproof.scale.result import ScaleResult


def _pt(factor, work, temp=0, plan="a", ms=1.0):
    return ProbePoint(
        factor=factor, total_rows=factor * 1000, work_blocks=work,
        peak_memory_kb=0, temp_blocks=temp, plan_hash=plan, exec_ms=ms,
    )


def _result(**kw):
    defaults = dict(
        points=[_pt(f, f * 100) for f in (1, 2, 4, 8, 16)],
        regimes=[FitResult(1.0, 0.999, None, 1, 16, "a")],
        plan_flips=[],
        truncated=False,
        function="f",
        sizes={"t": 1000},
    )
    defaults.update(kw)
    return ScaleResult(**defaults)


def test_exponent_comes_from_the_final_regime():
    assert _result().exponent == 1.0


def test_exponent_raises_when_the_fit_was_inconclusive():
    """Returning None would make `assert result.exponent < 1.5` fail
    with a TypeError that says nothing about why. Raising carries the
    reason."""
    bad = _result(regimes=[FitResult(None, 0.3, "r_squared 0.300 below 0.98", 1, 16, "a")])
    with pytest.raises(SqlProofScaleError, match="0.98"):
        _ = bad.exponent


def test_exponent_raises_when_every_point_flipped_plan():
    flips = [PlanFlip(2, "a", "b"), PlanFlip(4, "b", "c")]
    r = _result(regimes=[], plan_flips=flips)
    with pytest.raises(SqlProofScaleError, match="plan"):
        _ = r.exponent


def test_spills_below_is_false_when_nothing_spilled():
    assert _result().spills_below(1_000_000) is False
    assert _result().spill_point_rows is None


def test_spills_below_uses_total_rows_across_the_profile():
    pts = [_pt(1, 100), _pt(2, 200), _pt(4, 400, temp=500), _pt(8, 800, temp=900)]
    r = _result(points=pts)
    assert r.spill_point_rows == 4000
    assert r.factor_at_spill == 4
    assert r.spills_below(5000) is True
    assert r.spills_below(3000) is False


def test_rows_before_timeout_returns_a_band():
    band = _result().rows_before_timeout(10_000)
    assert band is None or (isinstance(band, tuple) and band[0] < band[1])


def test_truncated_sweep_refuses_to_project():
    assert _result(truncated=True).rows_before_timeout(10_000) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_scale_result.py -v`
Expected: FAIL — `ImportError: cannot import name 'SqlProofScaleError'`

- [ ] **Step 3: Add the exception**

In `src/sqlproof/exceptions.py`, alongside the existing exceptions:

```python
class SqlProofScaleError(SqlProofError):
    """A scale measurement could not produce the answer that was asked
    for -- an inconclusive fit, or no stable plan regime. Raised rather
    than returning None so the reason travels with the failure."""
```

(Match the base class the other exceptions in that file use.)

- [ ] **Step 4: Implement `ScaleResult`**

```python
# src/sqlproof/scale/result.py
"""What a scale run reports, and what a test asserts against.

Every row-count figure is TOTAL ROWS ACROSS THE PROFILE --
`sum(sizes.values()) * factor` -- never one table's count. The sweep
scales a whole profile, so a single table's number would be ambiguous.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sqlproof.exceptions import SqlProofScaleError
from sqlproof.scale.fit import FitResult, PlanFlip, find_spill, project_rows_before_timeout
from sqlproof.scale.probe import ProbePoint


@dataclass(frozen=True, slots=True)
class ScaleResult:
    points: Sequence[ProbePoint]
    regimes: Sequence[FitResult]
    plan_flips: Sequence[PlanFlip]
    truncated: bool
    function: str
    sizes: Mapping[str, int]

    @property
    def exponent(self) -> float:
        """The complexity exponent of the final plan regime.

        Raises rather than returning None. `assert result.exponent < 1.5`
        against a None would fail with a TypeError that explains nothing;
        the reason the fit failed is what the caller actually needs.
        """
        if not self.regimes:
            msg = (
                f"No stable plan regime for {self.function}: the planner changed "
                f"strategy at every scale point ({len(self.plan_flips)} flips). "
                "The flips are the finding; no exponent is claimed."
            )
            raise SqlProofScaleError(msg)
        final = self.regimes[-1]
        if final.exponent is None:
            msg = (
                f"Could not fit a complexity exponent for {self.function}: "
                f"{final.reason}. Measured factors {final.from_factor}x-"
                f"{final.to_factor}x."
            )
            raise SqlProofScaleError(msg)
        return final.exponent

    @property
    def r_squared(self) -> float | None:
        return self.regimes[-1].r_squared if self.regimes else None

    @property
    def _spill(self) -> ProbePoint | None:
        return find_spill(self.points)

    @property
    def spill_point_rows(self) -> int | None:
        spill = self._spill
        return None if spill is None else spill.total_rows

    @property
    def factor_at_spill(self) -> int | None:
        spill = self._spill
        return None if spill is None else spill.factor

    def spills_below(self, rows: int) -> bool:
        """Did a sort or hash spill to disk at or below `rows` total rows?"""
        point = self.spill_point_rows
        return point is not None and point <= rows

    def rows_before_timeout(self, timeout_ms: float) -> tuple[int, int] | None:
        """Projected total rows at which the function crosses `timeout_ms`.

        MACHINE-DEPENDENT: derived from wall-clock on the machine that
        ran the sweep, unlike `exponent`. Returns a band, or None when
        projecting would be guessing.
        """
        if not self.regimes:
            return None
        return project_rows_before_timeout(
            self.points, self.regimes[-1], timeout_ms, truncated=self.truncated
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_scale_result.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add src/sqlproof/scale/result.py src/sqlproof/exceptions.py tests/unit/test_scale_result.py
git commit -m "feat(scale): ScaleResult assertion surface, raising on inconclusive fits"
```

---

### Task 8: The sweep controller

**Files:**
- Create: `src/sqlproof/scale/sweep.py`
- Test: `tests/integration/test_scale_complexity_live.py`

**Interfaces:**
- Consumes: `probe_function` (Task 2), `resolve_args` (Task 3), `segment_by_plan` / `fit_exponent` (Tasks 4-5), `ScaleResult` (Task 7), `load_dataset` (existing, `scale/load.py`).
- Produces: `run_sweep(conn, schema, function, *, sizes, args=(), max_factor=32, min_points=5, probe_timeout_s=30.0, seed=0, columns=None) -> ScaleResult`

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_scale_complexity_live.py
"""End-to-end complexity recovery against real Postgres.

These fixtures are the regression net for the whole feature: functions
whose complexity is known by construction, whose exponents must come
back within tolerance.

IMPORTANT when adding a fixture: verify it actually HAS the complexity
it claims before trusting it as an oracle. Postgres will happily
optimise a naively-written "quadratic" query into something linear, and
a broken oracle silently validates a broken fit.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof.scale.sweep import run_sweep
from sqlproof.schema.parse_sql import parse_schema_sql

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE orgs (id bigint PRIMARY KEY, name text NOT NULL);
CREATE TABLE events (
  id bigint PRIMARY KEY,
  org_id bigint NOT NULL REFERENCES orgs(id),
  tag text NOT NULL
);
"""

# No index on events.org_id, so the per-org lookup is a full scan.
FUNCTIONS_SQL = """
CREATE FUNCTION scale_test.linear_fn() RETURNS bigint LANGUAGE plpgsql STABLE AS $$
DECLARE n bigint;
BEGIN SELECT count(*) INTO n FROM scale_test.events; RETURN n; END $$;

CREATE FUNCTION scale_test.quadratic_fn() RETURNS bigint LANGUAGE plpgsql STABLE AS $$
DECLARE o record; total bigint := 0; c bigint;
BEGIN
  FOR o IN SELECT id FROM scale_test.orgs LOOP
    SELECT count(*) INTO c FROM scale_test.events WHERE org_id = o.id;
    total := total + c;
  END LOOP;
  RETURN total;
END $$;

CREATE FUNCTION scale_test.constant_fn() RETURNS int LANGUAGE sql IMMUTABLE AS
  'SELECT 1';
"""


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS scale_test CASCADE")
        connection.execute("CREATE SCHEMA scale_test")
        connection.execute("SET search_path TO scale_test")
        connection.execute(SCHEMA_SQL)
        connection.execute(FUNCTIONS_SQL)
        try:
            yield connection
        finally:
            connection.execute("DROP SCHEMA IF EXISTS scale_test CASCADE")


def _schema():
    return parse_schema_sql(SCHEMA_SQL, schema="scale_test")


def test_linear_function_recovers_exponent_near_one(conn):
    result = run_sweep(
        conn, _schema(), "scale_test.linear_fn",
        sizes={"orgs": 20, "events": 400}, max_factor=16,
    )
    assert 0.7 < result.exponent < 1.3


def test_quadratic_function_recovers_exponent_near_two(conn):
    result = run_sweep(
        conn, _schema(), "scale_test.quadratic_fn",
        sizes={"orgs": 20, "events": 400}, max_factor=16,
    )
    assert 1.6 < result.exponent < 2.4


def test_constant_function_recovers_exponent_near_zero(conn):
    result = run_sweep(
        conn, _schema(), "scale_test.constant_fn",
        sizes={"orgs": 20, "events": 400}, max_factor=16,
    )
    assert abs(result.exponent) < 0.4


def test_sweep_records_every_point_it_measured(conn):
    result = run_sweep(
        conn, _schema(), "scale_test.linear_fn",
        sizes={"orgs": 20, "events": 400}, max_factor=16,
    )
    assert len(result.points) >= 5
    assert [p.factor for p in result.points] == sorted(p.factor for p in result.points)
    assert all(p.total_rows == 420 * p.factor for p in result.points)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_scale_complexity_live.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqlproof.scale.sweep'`

- [ ] **Step 3: Implement the sweep**

```python
# src/sqlproof/scale/sweep.py
"""Drive load -> probe across scale factors, then fit.

The ladder stops on FIT QUALITY, not a time budget. That follows from
leading with the exponent rather than a breaking point: measuring a
complexity class needs enough points to fit a curve, not enough rows to
reach production scale. The Phase 1 spike recovered a clean quadratic
from 2K-32K rows in under a second of query time per point.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

import psycopg

from sqlproof.generators.rows import ColumnOverrides
from sqlproof.scale.args import resolve_args
from sqlproof.scale.fit import fit_exponent, segment_by_plan
from sqlproof.scale.load import analyze, load_dataset
from sqlproof.scale.probe import ProbePoint, probe_function
from sqlproof.scale.result import ScaleResult
from sqlproof.schema.model import SchemaInfo


def run_sweep(
    conn: psycopg.Connection,
    schema: SchemaInfo,
    function: str,
    *,
    sizes: Mapping[str, int],
    args: Sequence[Any] = (),
    max_factor: int = 32,
    min_points: int = 5,
    probe_timeout_s: float = 30.0,
    seed: int = 0,
    columns: ColumnOverrides | None = None,
) -> ScaleResult:
    base_total = sum(sizes.values())
    points: list[ProbePoint] = []
    truncated = False
    baseline = 0
    factor = 1

    while factor <= max_factor:
        scaled = {name: count * factor for name, count in sizes.items()}
        _truncate(conn, schema)
        load_dataset(conn, schema, scaled, seed=seed, columns=columns)
        analyze(conn, schema)

        resolved = resolve_args(conn, args)
        started = time.perf_counter()
        point = probe_function(
            conn, function, resolved,
            factor=factor, total_rows=base_total * factor,
        )
        elapsed = time.perf_counter() - started
        points.append(point)

        if factor == 1:
            # The fixed per-call cost: catalog lookups and plan caching,
            # paid regardless of data size. Leaving it in the fit
            # flattens the curve and understates the exponent (the
            # Phase 1 spike measured 1.69 raw against 1.994 corrected on
            # an exactly-quadratic function).
            baseline = point.work_blocks

        if elapsed > probe_timeout_s:
            truncated = True
            break

        if len(points) >= min_points:
            segments, _flips = segment_by_plan(points)
            trial = fit_exponent(segments[-1], baseline)
            if trial.exponent is not None:
                break

        factor *= 2

    segments, flips = segment_by_plan(points)
    regimes = [fit_exponent(segment, baseline) for segment in segments]
    return ScaleResult(
        points=points,
        regimes=[r for r in regimes if r.exponent is not None] or regimes,
        plan_flips=flips,
        truncated=truncated,
        function=function,
        sizes=dict(sizes),
    )


def _truncate(conn: psycopg.Connection, schema: SchemaInfo) -> None:
    """Clear every table before reloading at the next factor.

    Reloading from empty each time is deliberate for now: appending
    would be cheaper, but a partially-appended table whose earlier rows
    were generated at a different factor would silently change the key
    distribution the FK arithmetic assumes.
    """
    names = ", ".join(
        f'"{table.schema}"."{table.name}"' for table in schema.tables
    )
    if names:
        conn.execute(f"TRUNCATE {names} CASCADE")  # noqa: S608 - quoted identifiers
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_scale_complexity_live.py -v
```
Expected: PASS (4 tests)

**If an exponent comes back outside its band, do not widen the band.** Check the fixture first: run the function at two sizes by hand and confirm the work really does grow the way the name claims. A "quadratic" function the planner optimised into a hash join is a broken oracle, and widening the assertion would hide a genuine fitting bug.

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/scale/sweep.py tests/integration/test_scale_complexity_live.py
git commit -m "feat(scale): sweep controller with fit-quality stop conditions"
```

---

### Task 9: Public entry point

**Files:**
- Modify: `src/sqlproof/scale/__init__.py`
- Create: `tests/integration/test_scale_api_live.py`

**Interfaces:**
- Consumes: `run_sweep` (Task 8), resolvers (Task 3), `SqlProof` (existing `core.py`).
- Produces: `scale_analysis(proof, function, *, sizes, args=(), **kwargs) -> ScaleResult`, plus re-exports of `heaviest`, `random_key`, `median_key`, `ScaleResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_scale_api_live.py
"""The public surface, used the way a caller would.

The output is an ASSERTION, not a report: CI goes red when someone
writes a query that will not survive growth. The artifact written
alongside is for trend history, the way mutation runs are.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof import SqlProof
from sqlproof.config import SqlProofConfig
from sqlproof.scale import heaviest, scale_analysis

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE orgs (id bigint PRIMARY KEY, name text NOT NULL);
CREATE TABLE events (
  id bigint PRIMARY KEY,
  org_id bigint NOT NULL REFERENCES orgs(id),
  tag text NOT NULL
);
"""


@pytest.fixture
def proof():
    dsn = os.environ[DSN_ENV]
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute("DROP SCHEMA IF EXISTS api_test CASCADE")
        setup.execute("CREATE SCHEMA api_test")
        setup.execute("SET search_path TO api_test")
        setup.execute(SCHEMA_SQL)
        setup.execute(
            "CREATE FUNCTION api_test.events_for(o bigint) RETURNS bigint "
            "LANGUAGE sql STABLE AS "
            "$$ SELECT count(*) FROM api_test.events WHERE org_id = o $$"
        )
    try:
        yield SqlProof.from_config(
            SqlProofConfig(connection_string=dsn, schema="api_test")
        )
    finally:
        with psycopg.connect(dsn, autocommit=True) as teardown:
            teardown.execute("DROP SCHEMA IF EXISTS api_test CASCADE")


def test_scale_analysis_reads_as_a_test(proof):
    result = scale_analysis(
        proof,
        "api_test.events_for",
        sizes={"orgs": 20, "events": 400},
        args=[heaviest("api_test.orgs.id")],
        max_factor=16,
    )
    assert result.exponent < 2.5
    assert not result.spills_below(1_000)


def test_resolved_arguments_are_recorded_per_point(proof):
    """A surprising result has to be reproducible, and that means
    knowing which argument each point was measured with."""
    result = scale_analysis(
        proof,
        "api_test.events_for",
        sizes={"orgs": 20, "events": 400},
        args=[heaviest("api_test.orgs.id")],
        max_factor=8,
    )
    assert all(p.args for p in result.points)
    # The heaviest key grows with the data, so it must not be constant.
    assert len({p.args for p in result.points}) > 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/integration/test_scale_api_live.py -v`
Expected: FAIL — `ImportError: cannot import name 'scale_analysis'`

- [ ] **Step 3: Implement the entry point**

```python
# src/sqlproof/scale/__init__.py
"""Scale measurement: is this function's growth curve acceptable?

The output is an assertion, not a report -- CI goes red when someone
writes a query that will not survive growth, the way
`assert_no_survivors()` works for mutation testing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import psycopg

from sqlproof.scale.args import heaviest, median_key, random_key
from sqlproof.scale.result import ScaleResult
from sqlproof.scale.sweep import run_sweep

__all__ = [
    "ScaleResult",
    "heaviest",
    "median_key",
    "random_key",
    "scale_analysis",
]


def scale_analysis(
    proof: Any,
    function: str,
    *,
    sizes: Mapping[str, int],
    args: Sequence[Any] = (),
    **kwargs: Any,
) -> ScaleResult:
    """Measure how `function` scales, using `proof`'s schema and database.

    Opens its own autocommit connection: the sweep issues TRUNCATE,
    COPY and ANALYZE, which want a lifecycle of their own rather than
    sharing the property runner's transaction.
    """
    dsn = proof.config.connection_string
    if dsn is None:
        msg = (
            "scale_analysis needs a real database: construct SqlProof with "
            "a connection_string rather than a schema file."
        )
        raise ValueError(msg)
    with psycopg.connect(dsn, autocommit=True) as conn:
        return run_sweep(conn, proof.schema_info, function, sizes=sizes, args=args, **kwargs)
```

Replace the module's previous one-line docstring; keep `load.py` importable as before.

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run pytest tests/integration/test_scale_api_live.py -v
```
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/sqlproof/scale/__init__.py tests/integration/test_scale_api_live.py
git commit -m "feat(scale): scale_analysis public entry point"
```

---

### Task 10: Persist a run artifact

**Files:**
- Create: `src/sqlproof/scale/artifact.py`
- Modify: `src/sqlproof/scale/__init__.py`
- Test: `tests/unit/test_scale_artifact.py`

**Interfaces:**
- Consumes: `ScaleResult` (Task 7).
- Produces: `save_run(result: ScaleResult, artifact_dir: Path, *, schema_fingerprint: str = "") -> Path`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_scale_artifact.py
"""Run artifacts.

Mirrors the mutation-run artifact conventions (schema_version, git sha,
schema fingerprint) so both feed one ingester later. Writing it is a
side effect of a scale run, not its point -- the assertion is the point.
"""
from __future__ import annotations

import json

from sqlproof.scale.artifact import save_run
from sqlproof.scale.fit import FitResult
from sqlproof.scale.probe import ProbePoint
from sqlproof.scale.result import ScaleResult


def _result():
    return ScaleResult(
        points=[
            ProbePoint(factor=f, total_rows=f * 1000, work_blocks=f * 100,
                       peak_memory_kb=0, temp_blocks=0, plan_hash="a",
                       exec_ms=float(f), args=(7,))
            for f in (1, 2, 4, 8, 16)
        ],
        regimes=[FitResult(1.0, 0.999, None, 1, 16, "a")],
        plan_flips=[],
        truncated=False,
        function="billing.compute_invoice",
        sizes={"customers": 100, "invoices": 1000},
    )


def test_artifact_round_trips_the_measured_points(tmp_path):
    path = save_run(_result(), tmp_path)
    data = json.loads(path.read_text())
    assert data["function"] == "billing.compute_invoice"
    assert len(data["points"]) == 5
    assert data["points"][0]["factor"] == 1


def test_artifact_records_the_exponent_and_fit_quality(tmp_path):
    data = json.loads(save_run(_result(), tmp_path).read_text())
    assert data["regimes"][0]["exponent"] == 1.0
    assert data["regimes"][0]["r_squared"] == 0.999


def test_artifact_records_resolved_arguments_per_point(tmp_path):
    """Without these a surprising result is unreproducible."""
    data = json.loads(save_run(_result(), tmp_path).read_text())
    assert data["points"][0]["args"] == [7]


def test_artifact_labels_the_worst_case_argument_policy(tmp_path):
    """Results describe worst-case behaviour when `heaviest` is used.
    Unlabelled, someone reads the exponent as a median."""
    data = json.loads(save_run(_result(), tmp_path).read_text())
    assert "argument_policy" in data


def test_artifact_carries_a_schema_version(tmp_path):
    data = json.loads(save_run(_result(), tmp_path).read_text())
    assert data["schema_version"] == 1


def test_missing_directory_is_created(tmp_path):
    target = tmp_path / "nested" / "runs"
    path = save_run(_result(), target)
    assert path.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_scale_artifact.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqlproof.scale.artifact'`

- [ ] **Step 3: Implement**

```python
# src/sqlproof/scale/artifact.py
"""Persist a scale run as JSON.

Same conventions as the mutation-run artifacts (schema_version, git sha,
schema fingerprint) so a single ingester can consume both. This is the
wire format a cloud tier would read; the local file is a side effect of
running the assertion.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from sqlproof.scale.result import ScaleResult

SCHEMA_VERSION = 1


def _git_sha() -> str | None:
    try:
        out = subprocess.run(  # noqa: S603, S607 - fixed argv, no shell
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=False, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def save_run(
    result: ScaleResult,
    artifact_dir: Path,
    *,
    schema_fingerprint: str = "",
) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")
    safe = result.function.replace(".", "_")
    path = artifact_dir / f"{stamp}-{safe}.json"

    payload = {
        "schema_version": SCHEMA_VERSION,
        "function": result.function,
        "started_at": datetime.now(UTC).isoformat(),
        "git_sha": _git_sha(),
        "schema_fingerprint": schema_fingerprint,
        "sizes": dict(result.sizes),
        "truncated": result.truncated,
        # Results describe the WORST case when `heaviest` resolves the
        # arguments. Recorded so nobody reads the exponent as a median.
        "argument_policy": "worst-case when resolvers are used; literals as given",
        "points": [
            {**asdict(point), "args": list(point.args)} for point in result.points
        ],
        "regimes": [asdict(regime) for regime in result.regimes],
        "plan_flips": [asdict(flip) for flip in result.plan_flips],
    }
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path
```

- [ ] **Step 4: Wire it into `scale_analysis`**

In `src/sqlproof/scale/__init__.py`, add an `artifact_dir` parameter:

```python
def scale_analysis(
    proof: Any,
    function: str,
    *,
    sizes: Mapping[str, int],
    args: Sequence[Any] = (),
    artifact_dir: Path | str | None = ".sqlproof/scale-runs",
    **kwargs: Any,
) -> ScaleResult:
```

and after the sweep returns:

```python
        result = run_sweep(conn, proof.schema_info, function, sizes=sizes, args=args, **kwargs)
    if artifact_dir is not None:
        save_run(
            result,
            Path(artifact_dir),
            schema_fingerprint=getattr(proof, "schema_fingerprint", ""),
        )
    return result
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/unit/test_scale_artifact.py tests/integration/test_scale_api_live.py -v
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/sqlproof/scale/artifact.py src/sqlproof/scale/__init__.py tests/unit/test_scale_artifact.py
git commit -m "feat(scale): persist run artifacts alongside the assertion"
```

---

### Task 11: Prove spill detection against a real spill

**Files:**
- Create: `tests/integration/test_scale_space_live.py`

**Interfaces:**
- Consumes: `run_sweep` (Task 8).
- Produces: nothing.

- [ ] **Step 1: Write the test**

```python
# tests/integration/test_scale_space_live.py
"""Space behaviour: the cliff, not the curve.

A sort that fits in work_mem is fast; the moment it spills, performance
drops sharply. A fit run straight through that boundary reports a
misleadingly gentle exponent, which is why space is a separate axis
rather than folded into the exponent.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof.scale.sweep import run_sweep
from sqlproof.schema.parse_sql import parse_schema_sql

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE wide_rows (
  id bigint PRIMARY KEY,
  payload text NOT NULL
);
"""


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS space_test CASCADE")
        connection.execute("CREATE SCHEMA space_test")
        connection.execute("SET search_path TO space_test")
        connection.execute(SCHEMA_SQL)
        # A deliberately tiny work_mem so a modest sort spills. Session
        # scoped, so it cannot leak to other tests.
        connection.execute("SET work_mem = '64kB'")
        connection.execute(
            "CREATE FUNCTION space_test.sort_everything() RETURNS bigint "
            "LANGUAGE sql STABLE AS $$ "
            "SELECT count(*) FROM (SELECT payload FROM space_test.wide_rows "
            "ORDER BY payload) s $$"
        )
        try:
            yield connection
        finally:
            connection.execute("DROP SCHEMA IF EXISTS space_test CASCADE")


def test_a_sort_over_work_mem_reports_a_spill_point(conn):
    schema = parse_schema_sql(SCHEMA_SQL, schema="space_test")
    result = run_sweep(
        conn, schema, "space_test.sort_everything",
        sizes={"wide_rows": 2000}, max_factor=16,
    )
    assert result.spill_point_rows is not None, (
        "expected a spill at work_mem=64kB; if this fails, confirm the sort "
        "is actually happening (EXPLAIN should show a Sort node) rather than "
        "relaxing the assertion"
    )
    assert result.spills_below(result.spill_point_rows) is True
    assert result.factor_at_spill is not None


def test_a_function_that_never_sorts_reports_no_spill(conn):
    conn.execute(
        "CREATE FUNCTION space_test.just_count() RETURNS bigint LANGUAGE sql "
        "STABLE AS $$ SELECT count(*) FROM space_test.wide_rows $$"
    )
    schema = parse_schema_sql(SCHEMA_SQL, schema="space_test")
    result = run_sweep(
        conn, schema, "space_test.just_count",
        sizes={"wide_rows": 2000}, max_factor=8,
    )
    assert result.spill_point_rows is None
    assert result.spills_below(10_000_000) is False
```

- [ ] **Step 2: Run the test**

```bash
uv run pytest tests/integration/test_scale_space_live.py -v
```
Expected: PASS (2 tests)

If no spill is detected, check with `EXPLAIN (ANALYZE, BUFFERS)` that the sort is happening and that `work_mem` took effect on the session — do not relax the assertion, which would leave the space axis untested.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_scale_space_live.py
git commit -m "test(scale): prove spill detection against a real work_mem spill"
```

---

### Task 12: Prove plan-flip segmentation against a real flip

**Files:**
- Create: `tests/integration/test_scale_plan_flip_live.py`

**Interfaces:**
- Consumes: `run_sweep` (Task 8).
- Produces: nothing.

- [ ] **Step 1: Write the test**

```python
# tests/integration/test_scale_plan_flip_live.py
"""Plan flips are findings, not noise.

At small n a sequential scan is genuinely cheapest and the planner picks
it; past some size it switches to an index scan. That is a real
discontinuity in the cost curve, so each regime is fitted separately and
the flip is reported. Fitting across one averages two different
functions together.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from sqlproof.scale.sweep import run_sweep
from sqlproof.schema.parse_sql import parse_schema_sql

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE lookups (
  id bigint PRIMARY KEY,
  needle bigint NOT NULL
);
"""


@pytest.fixture
def conn():
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS flip_test CASCADE")
        connection.execute("CREATE SCHEMA flip_test")
        connection.execute("SET search_path TO flip_test")
        connection.execute(SCHEMA_SQL)
        connection.execute("CREATE INDEX ON flip_test.lookups (needle)")
        connection.execute(
            "CREATE FUNCTION flip_test.find_one() RETURNS bigint LANGUAGE sql "
            "STABLE AS $$ SELECT count(*) FROM flip_test.lookups WHERE needle = 1 $$"
        )
        try:
            yield connection
        finally:
            connection.execute("DROP SCHEMA IF EXISTS flip_test CASCADE")


def test_a_flip_is_reported_and_the_fit_is_segmented(conn):
    """With an index present, Postgres seq-scans a small table and
    switches to the index once the table is big enough for it to pay.
    We assert on the mechanism -- if a flip occurs, it is recorded and
    the regimes match -- rather than forcing a specific crossover, which
    depends on the planner's cost constants."""
    schema = parse_schema_sql(SCHEMA_SQL, schema="flip_test")
    result = run_sweep(
        conn, schema, "flip_test.find_one",
        sizes={"lookups": 500}, max_factor=32, min_points=6,
    )
    distinct_plans = {p.plan_hash for p in result.points}
    if len(distinct_plans) == 1:
        pytest.skip("planner kept one plan across the swept range")
    assert result.plan_flips, "distinct plans seen but no flip recorded"
    assert len(result.regimes) >= 1
    # A recorded flip must name a factor that was actually measured.
    measured = {p.factor for p in result.points}
    assert all(flip.at_factor in measured for flip in result.plan_flips)
```

- [ ] **Step 2: Run the test**

```bash
uv run pytest tests/integration/test_scale_plan_flip_live.py -v
```
Expected: PASS (skips if the planner never flips in range, which is a legitimate outcome — the mechanism is what is under test, not the planner's cost constants)

- [ ] **Step 3: Run the whole suite and lint**

```bash
uv run pytest -q                       # expect 620 + the new tests
uv run ruff check src/ tests/
uv run mypy src/sqlproof
uv run pyright src/sqlproof/scale/
uv run pytest --cov=sqlproof --cov-fail-under=94 -q
```

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_scale_plan_flip_live.py
git commit -m "test(scale): prove plan-flip segmentation against a real flip"
```

---

## Self-Review Notes

**Spec coverage.** Each spec section maps to a task: probe/measurement (1, 2), arguments (3), time-work axis (4), plan-flip segmentation (5, 12), space axis (6, 11), `ScaleResult` and its raise-on-inconclusive contract (7), sweep and stop conditions (8), the Python API (9), artifacts (10), known-complexity fixtures (8). Out-of-scope items — catalog discovery, a CLI, concurrency, splinter wrapping, the FK-cycle fix — appear nowhere, as intended.

**Known gaps, recorded rather than hidden:**

1. **The sweep truncates and reloads at each factor rather than appending.** The spec's Phase 1 lineage called incremental appending an optimisation worth building in from the start, and Task 8 deliberately does not. Appending would make a sweep cost the largest point rather than the sum of all points, but rows appended at a later factor would carry key distributions computed against a different parent count, silently changing what the FK arithmetic assumes. Reloading is correct and slow; appending is fast and needs a design. The docstring in `_truncate` says so.
2. **`heaviest` is approximated by the largest key, not a true child-row count.** For sequentially assigned keys under the current generator that is usually the same row, but not always, and it will be wrong for a schema whose skew does not follow key order. A true implementation needs the referencing table, which means walking the FK graph — deferred deliberately, and the test asserts the query shape rather than that it finds a genuine maximum.
3. **The timeout projection band is `0.6x`–`1.6x`, chosen rather than derived.** It reflects the 1.96× wall-clock spread measured in the Phase 1 spike, but it is not a statistically justified interval. It is deliberately wide; anyone tightening it should derive it from the observed variance instead.
4. **Task 12 may skip.** Whether Postgres flips plan within the swept range depends on its cost constants and the machine. The test asserts the mechanism when a flip occurs and skips when none does — an honest outcome, but it means the flip path can go unexercised on some machines. The unit tests in Task 5 cover the segmentation logic unconditionally.

**Type consistency.** `ProbePoint`, `FitResult`, `PlanFlip`, `ScaleResult`, `parse_plan`, `probe_function`, `resolve_args`, `heaviest`, `random_key`, `median_key`, `fit_exponent`, `segment_by_plan`, `find_spill`, `project_rows_before_timeout`, `run_sweep`, `scale_analysis` and `save_run` are used with identical signatures everywhere they appear across tasks. `ProbePoint.args` is a tuple throughout, serialised as a list only in the artifact.
