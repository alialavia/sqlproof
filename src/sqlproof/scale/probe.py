"""One measurement of a function under EXPLAIN.

Work is measured in buffer pages touched, not wall-clock. Measured in
the Phase 1 spike: at a fixed dataset, wall-clock varied 1.96x across
runs on an *idle* machine while buffer counts varied 1.0007x. An
exponent fitted on buffers holds on a laptop and a noisy CI runner
alike; fitted on wall-clock, the gate would flake.

The price is that buffers count I/O-visible work only. CPU spent on rows
already in memory -- a Materialize replayed for every outer row --
touches no buffer, so CPU-only growth is invisible here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, cast

from sqlproof import _database as database
from sqlproof.exceptions import SqlProofUsageError
from sqlproof.scale._identifiers import validate_function_name


@dataclass(frozen=True, slots=True)
class ProbePoint:
    """One scale point, measured from `EXPLAIN (ANALYZE, BUFFERS)` of
    `SELECT function(...)`.

    `work_blocks` counts the buffers of the relations `probe_function`
    was given (as `run_sweep` passes its tables), or, without them, the
    root node's total. Either way it includes every statement run inside
    the function; so does `temp_blocks`, the root's total. `plan_hash` and
    `peak_memory_kb` describe only the OUTER statement's plan: for a
    function Postgres does not inline -- every plpgsql function, and SQL
    functions with a FROM clause or an aggregate -- that plan is a single
    `Result` node, so `plan_hash` is the same at every scale and
    `peak_memory_kb` is always 0.
    """

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
    work = _work(root)
    memory = _peak_memory(root)
    temp = _temp_blocks(root)
    shape = _shape(root)
    digest = hashlib.sha256(shape.encode()).hexdigest()[:16]
    return work, memory, temp, digest, float(envelope.get("Execution Time", 0.0))


def _work(root: dict[str, Any]) -> int:
    """Buffer pages touched by the whole query: the ROOT node's counts.

    Two properties of EXPLAIN output make this a one-liner rather than a
    tree walk, and both were verified against a live nested loop
    (Aggregate=24, Nested Loop=24, Seq Scan=23, Materialize loops=5000
    sharedHit=1):

    1. Buffer counts are CUMULATIVE TOTALS, not per-loop averages. The
       Materialize executed 5000 times and reports 1 block. This is the
       opposite of `Actual Rows` and `Actual Time`, which ARE per-loop
       averages -- a genuine trap, but for anything derived from rows,
       not from buffers.
    2. They are INCLUSIVE of children. The root already contains every
       descendant's count.

    So summing the tree double-counts, and multiplying by loops inflates
    by a factor that GROWS WITH n -- which would fabricate superlinear
    growth from a linear function and report a false quadratic. Take the
    root.
    """
    return int(root.get("Shared Hit Blocks", 0)) + int(
        root.get("Shared Read Blocks", 0)
    )


def _peak_memory(node: dict[str, Any]) -> int:
    """Largest single node's memory, not the sum.

    Peak memory is what decides whether `work_mem` is exceeded, and
    nodes do not all hold their peak simultaneously. Summing would
    overstate it and make the spill point look closer than it is.

    A sort that spilled reports its DISK usage in the same "Sort Space
    Used" field, marked "Sort Space Type": "Disk". That space is temp
    blocks, not memory, so it is not counted here.
    """
    sort_space = int(node.get("Sort Space Used", 0))
    if node.get("Sort Space Type") == "Disk":
        sort_space = 0
    own = max(sort_space, int(node.get("Peak Memory Usage", 0)))
    for child in node.get("Plans", []):
        own = max(own, _peak_memory(child))
    return own


def _temp_blocks(root: dict[str, Any]) -> int:
    """Root total, for the same reason `_work` takes the root: temp
    blocks live in the same BufferUsage struct and accumulate inclusively
    up the tree."""
    return int(root.get("Temp Read Blocks", 0)) + int(
        root.get("Temp Written Blocks", 0)
    )


def _shape(node: dict[str, Any]) -> str:
    """Node types and nesting only.

    Deliberately excludes row counts, costs and timings: the point is to
    detect the planner switching STRATEGY, not the data changing size.
    Every scale point changes the numbers; only a real plan change
    should change this hash.

    The hash covers only the OUTER statement of `SELECT fn()`. Statements
    run inside a non-inlined function never appear in its EXPLAIN, so a
    plan change there leaves this hash unchanged (Ruling AK).
    """
    children = ",".join(_shape(child) for child in node.get("Plans", []))
    return f"{node.get('Node Type', '?')}({children})"


# auto_explain, set for the probe's call only: every statement the
# function runs, at any depth, is reported as a JSON plan in a notice.
# `log_timing = off` keeps per-node clocks out of it; `log_analyze` is
# still needed for sort and hash memory.
_NESTED_PLAN_SETTINGS = (
    "auto_explain.log_min_duration = 0",
    "auto_explain.log_nested_statements = on",
    "auto_explain.log_analyze = on",
    "auto_explain.log_timing = off",
    "auto_explain.log_buffers = off",
    "auto_explain.log_format = json",
    "auto_explain.log_level = notice",
)


class _NestedPlans:
    """Collects the shapes and peak memory of the plans auto_explain
    reports. Distinct shapes only: a function that runs one query a
    thousand times reports a thousand plans of one shape."""

    def __init__(self) -> None:
        self.shapes: set[str] = set()
        self.peak_memory_kb = 0

    def receive(self, message: str) -> bool:
        start = message.find("{")
        if "plan:" not in message[:start] or start < 0:
            return False
        try:
            plan = json.loads(message[start:])["Plan"]
        except (ValueError, KeyError, TypeError):
            return False
        self.shapes.add(_shape(plan))
        self.peak_memory_kb = max(self.peak_memory_kb, _peak_memory(plan))
        return True


def _enable_nested_plans(cur: Any) -> None:
    """Turn auto_explain on for this transaction, loading it if the server
    has not preloaded it, or raise saying why it cannot be."""
    cur.execute("SAVEPOINT sqlproof_auto_explain")
    try:
        cur.execute(
            "SELECT current_setting('auto_explain.log_min_duration', true) IS NOT NULL"
            " AS loaded"
        )
        if not _first(cur.fetchone()):
            cur.execute("LOAD 'auto_explain'")
        for setting in _NESTED_PLAN_SETTINGS:
            cur.execute(f"SET LOCAL {setting}")
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT sqlproof_auto_explain")
        msg = (
            "nested_plans needs the auto_explain module, which this connection "
            f"cannot use ({str(exc).strip()}). It works as a superuser, or on a server that "
            "preloads it and lets this role set its options (the Supabase "
            "images do)."
        )
        raise SqlProofUsageError(msg) from exc
    cur.execute("RELEASE SAVEPOINT sqlproof_auto_explain")


# SQLSTATE 57014: the statement was cancelled (here, by statement_timeout).
_QUERY_CANCELED = "57014"


class ProbeTimedOut(Exception):
    """Postgres cancelled the probe at its timeout."""

    def __init__(self, function: str, timeout_s: float) -> None:
        super().__init__(f"{function} did not return within {timeout_s:g}s and was cancelled")
        self.function = function
        self.timeout_s = timeout_s


def probe_function(
    conn: database.Connection,
    function: str,
    args: Sequence[Any],
    *,
    factor: int,
    total_rows: int,
    relations: Sequence[tuple[str, str]] | None = None,
    timeout_s: float | None = None,
    nested_plans: bool = False,
) -> ProbePoint:
    """Measure one call of `function` under EXPLAIN.

    With `nested_plans`, the plans of the statements the function runs
    count too (scale follow-up 8): auto_explain reports each one during
    the call, and their shapes join the outer plan's in `plan_hash`, and
    their sort and hash memory in `peak_memory_kb`. So a plan change
    inside a PL/pgSQL or non-inlined SQL function splits the fit like one
    outside it. It instruments every inner statement, which slows calls
    that run many, so it is off by default; it raises
    `SqlProofUsageError` when the connection cannot use auto_explain.

    With `relations` -- ``(schema, table)`` pairs, as `run_sweep` passes
    the tables it scales -- `work_blocks` counts only buffer accesses to
    those tables, their indexes and their TOAST storage, read from this
    backend's per-relation counters for the current transaction. Without
    it, `work_blocks` is the EXPLAIN root's total, which also counts the
    system catalogs: when another session changes the catalogs (on a
    shared server, e.g. background workers starting for a new database),
    this backend re-reads its catalog caches inside the probe, and the
    count moves by tens of blocks with nothing about `function` changed.
    The per-relation counters belong to this backend alone and never
    include catalog pages, so other sessions cannot move them.

    Runs inside a savepoint that is always rolled back: a function with
    side effects must not change the data the next scale point measures
    against. Mirrors the isolation `core.py:175-179` uses for property
    runs.

    SAVEPOINT requires an open transaction, which an autocommit
    connection (e.g. the one integration tests hand in) does not have
    between statements. If none is open, one is opened here and rolled
    back afterwards too, so it leaves no trace either. If the connection
    is already inside a transaction -- e.g. DBManager's, autocommit=False
    -- that transaction is left untouched; only the savepoint rolls
    back, so callers can compose several probes inside one transaction.

    `timeout_s` makes Postgres cancel the call when it runs longer, and
    raises `ProbeTimedOut`. It is set with `SET LOCAL statement_timeout`
    after the savepoint, so rolling back to the savepoint restores the
    previous timeout.

    `function` is interpolated into the statement, so it is checked
    first -- every dot-separated part a bare identifier -- and a bad name
    raises `SqlProofUsageError` before any SQL runs (see
    `_identifiers.validate_function_name`).
    """
    validate_function_name(function)
    placeholders = ", ".join(["%s"] * len(args))
    statement = (
        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT {function}({placeholders})"
    )
    opened_transaction = not conn.in_transaction
    relation_blocks: int | None = None
    nested = _NestedPlans() if nested_plans else None
    with conn.cursor() as cur:
        if opened_transaction:
            cur.execute("BEGIN")
        cur.execute("SAVEPOINT sqlproof_probe")
        try:
            oids = _relation_oids(cur, relations) if relations is not None else None
            # The counters accumulate until the backend flushes them, which
            # never happens inside a transaction: the difference across the
            # call, both reads in this one transaction, is exactly its work.
            before = _blocks_fetched(cur, oids) if oids is not None else 0
            if timeout_s is not None:
                cur.execute(f"SET LOCAL statement_timeout = {max(1, round(timeout_s * 1000))}")
            if nested is not None:
                _enable_nested_plans(cur)
            try:
                with conn.notices(nested.receive) if nested is not None else nullcontext():
                    cur.execute(statement, tuple(args) or None)
                    row: Any = cur.fetchone()
                    if nested is not None:
                        cur.execute("SET LOCAL auto_explain.log_min_duration = -1")
            except Exception as exc:
                if timeout_s is not None and database.sqlstate(exc) == _QUERY_CANCELED:
                    raise ProbeTimedOut(function, timeout_s) from exc
                raise
            if timeout_s is not None:
                cur.execute("SET LOCAL statement_timeout = 0")
            if oids is not None:
                relation_blocks = _blocks_fetched(cur, oids) - before
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT sqlproof_probe")
            cur.execute("RELEASE SAVEPOINT sqlproof_probe")
            if opened_transaction:
                cur.execute("ROLLBACK")
    assert row is not None, "EXPLAIN ANALYZE always returns exactly one row"
    # PyGreSQL returns the JSON already decoded; the row is a 1-tuple (or
    # a 1-key dict under dict_row) holding the EXPLAIN array. `cast(Any,
    # row)` (rather than casting the branch result) keeps pyright from
    # narrowing the dict branch to `dict[Unknown, Unknown]`.
    explain_json = cast(
        "list[dict[str, Any]]",
        next(iter(cast(Any, row).values())) if isinstance(row, dict) else row[0],
    )
    work, memory, temp, digest, ms = parse_plan(explain_json)
    if nested is not None and nested.shapes:
        # The outer plan is among the reported ones (auto_explain sees the
        # top-level statement too), so the sorted set covers both.
        combined = "|".join(sorted({_shape(explain_json[0]["Plan"]), *nested.shapes}))
        digest = hashlib.sha256(combined.encode()).hexdigest()[:16]
        memory = max(memory, nested.peak_memory_kb)
    return ProbePoint(
        factor=factor,
        total_rows=total_rows,
        work_blocks=work if relation_blocks is None else relation_blocks,
        peak_memory_kb=memory,
        temp_blocks=temp,
        plan_hash=digest,
        exec_ms=ms,
        args=tuple(args),
    )


def _relation_oids(cur: Any, relations: Sequence[tuple[str, str]]) -> list[int]:
    """OIDs of `relations`, their indexes, their TOAST tables and the
    TOAST tables' indexes. Tables that do not exist are skipped."""
    names = [f"{schema}.{table}" for schema, table in relations]
    cur.execute(
        "WITH tables AS ("
        "  SELECT c.oid, c.reltoastrelid FROM pg_class c"
        "  JOIN pg_namespace n ON n.oid = c.relnamespace"
        "  WHERE n.nspname || '.' || c.relname = ANY(%s::text[])"
        "), heaps AS ("
        "  SELECT oid FROM tables"
        "  UNION SELECT reltoastrelid FROM tables WHERE reltoastrelid <> 0"
        ") SELECT oid::bigint FROM heaps"
        "  UNION SELECT indexrelid::bigint FROM pg_index"
        "  WHERE indrelid IN (SELECT oid FROM heaps)",
        (names,),
    )
    return sorted(int(_first(row)) for row in cur.fetchall())


def _blocks_fetched(cur: Any, oids: Sequence[int]) -> int:
    """Buffer requests (hits plus reads) this backend has made for `oids`
    and not yet flushed to the cumulative statistics."""
    if not oids:
        return 0
    cur.execute(
        "SELECT coalesce(sum(pg_stat_get_xact_blocks_fetched(oid)), 0)::bigint"
        " FROM unnest(%s::bigint[]::oid[]) AS oid",
        (list(oids),),
    )
    return int(_first(cur.fetchone()))


def _first(row: Any) -> Any:
    """A row's first value, whether the connection returns tuples or dicts."""
    # cast(Any, ...) keeps pyright from narrowing to dict[Unknown, Unknown],
    # as in probe_function.
    values: Any = cast(Any, row).values() if isinstance(row, dict) else row
    return next(iter(values))

