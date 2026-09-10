"""One measurement of a function under EXPLAIN.

Work is measured in buffer pages touched, not wall-clock. Measured in
the Phase 1 spike: at a fixed dataset, wall-clock varied 1.96x across
runs on an *idle* machine while buffer counts varied 1.0007x. An
exponent fitted on buffers holds on a laptop and a noisy CI runner
alike; fitted on wall-clock, the gate would flake.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, LiteralString, cast

import psycopg


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
    """
    own = max(
        int(node.get("Sort Space Used", 0)),
        int(node.get("Peak Memory Usage", 0)),
    )
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
    """
    children = ",".join(_shape(child) for child in node.get("Plans", []))
    return f"{node.get('Node Type', '?')}({children})"


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

    SAVEPOINT requires an open transaction, which an autocommit
    connection (e.g. the one integration tests hand in) does not have
    between statements. If none is open, one is opened here and rolled
    back afterwards too, so it leaves no trace either. If the connection
    is already inside a transaction -- e.g. DBManager's, autocommit=False
    -- that transaction is left untouched; only the savepoint rolls
    back, so callers can compose several probes inside one transaction.
    """
    placeholders = ", ".join(["%s"] * len(args))
    statement = (
        f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) SELECT {function}({placeholders})"
    )
    opened_transaction = (
        conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    )
    with conn.cursor() as cur:
        if opened_transaction:
            cur.execute("BEGIN")
        cur.execute("SAVEPOINT sqlproof_probe")
        try:
            # `statement` interpolates a runtime function name and
            # placeholder count, so it isn't a LiteralString; the cast
            # matches the pattern in scale/load.py and mutation/runner.py.
            cur.execute(cast(LiteralString, statement), tuple(args) or None)  # type: ignore[redundant-cast]
            # `conn` is typed as a bare, unparameterized `psycopg.Connection`
            # (callers use both a plain and a dict_row cursor), so pyright
            # cannot infer the row's shape; explicit `Any` launders that
            # instead of leaving it "Unknown" for every use below.
            row: Any = cur.fetchone()
        finally:
            cur.execute("ROLLBACK TO SAVEPOINT sqlproof_probe")
            cur.execute("RELEASE SAVEPOINT sqlproof_probe")
            if opened_transaction:
                cur.execute("ROLLBACK")
    assert row is not None, "EXPLAIN ANALYZE always returns exactly one row"
    # psycopg returns the JSON already decoded; the row is a 1-tuple (or
    # a 1-key dict under dict_row) holding the EXPLAIN array. `cast(Any,
    # row)` (rather than casting the branch result) keeps pyright from
    # narrowing the dict branch to `dict[Unknown, Unknown]`.
    explain_json = cast(
        "list[dict[str, Any]]",
        next(iter(cast(Any, row).values())) if isinstance(row, dict) else row[0],
    )
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
