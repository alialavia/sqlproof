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
from sqlproof.scale._identifiers import IDENTIFIER_RE

Resolver = Callable[[Any], Any]


def _split(column: str) -> tuple[str, str]:
    if "." not in column:
        msg = (
            f"Column reference {column!r} must be qualified as "
            '"table.column" so the resolver knows which table to query.'
        )
        raise SqlProofUsageError(msg)
    # These segments are interpolated into the resolver's SQL; see
    # `_identifiers.py` for why the rule is this strict, and fullmatch.
    for segment in column.split("."):
        if not IDENTIFIER_RE.fullmatch(segment):
            msg = (
                f"Column reference {column!r} contains an invalid "
                f"identifier segment {segment!r}. Each dot-separated part "
                "must be a bare SQL identifier (letters, digits and "
                "underscores, not starting with a digit) -- this string is "
                "interpolated directly into generated SQL."
            )
            raise SqlProofUsageError(msg)
    table, _, name = column.rpartition(".")
    return table, name


def _scalar(conn: Any, sql: str, column: str) -> Any:
    row = conn.execute(sql).fetchone()
    if row is None or row[0] is None:
        msg = (
            f"Resolver for {column!r} found no rows. Measuring an empty "
            "table would report the function as fast without testing it."
        )
        raise SqlProofUsageError(msg)
    return row[0]


def heaviest(column: str) -> Resolver:
    """The largest key value in the table, as a stand-in for "the key
    with the most referencing rows".

    This orders by the key column itself, descending -- it does not
    join out to count referencing rows in any child table. For the
    sequentially-assigned keys this project's bulk generator produces
    (`_unique_value` gives id = i + 1), the largest key and the most-
    referenced key are usually the same row, but not guaranteed to be:
    a true "most referencing rows" answer would need FK-graph
    traversal, which this deliberately does not build. Still a
    defensible worst-case pick even when the two diverge.
    """
    table, name = _split(column)

    def resolve(conn: Any) -> Any:
        # Identifiers come from the caller's schema (table/column names),
        # never from row data, so this interpolation is not user input.
        sql = f"SELECT {name} FROM {table} ORDER BY {name} DESC LIMIT 1"
        return _scalar(conn, sql, column)

    resolve._sqlproof_resolver = True  # type: ignore[attr-defined]
    return resolve


def random_key(column: str, *, seed: int = 0) -> Resolver:
    table, name = _split(column)

    def resolve(conn: Any) -> Any:
        # Identifiers come from the caller's schema; see `heaviest` above.
        sql = (
            f"SELECT {name} FROM {table} "
            f"ORDER BY md5({name}::text || '{seed}') LIMIT 1"
        )
        return _scalar(conn, sql, column)

    resolve._sqlproof_resolver = True  # type: ignore[attr-defined]
    return resolve


def median_key(column: str) -> Resolver:
    table, name = _split(column)

    def resolve(conn: Any) -> Any:
        # Identifiers come from the caller's schema; see `heaviest` above.
        sql = (
            f"SELECT {name} FROM {table} ORDER BY {name} "
            f"OFFSET (SELECT count(*) / 2 FROM {table}) LIMIT 1"
        )
        return _scalar(conn, sql, column)

    resolve._sqlproof_resolver = True  # type: ignore[attr-defined]
    return resolve


def resolve_args(conn: Any, args: Sequence[Any]) -> tuple[Any, ...]:
    """Resolve each entry: a callable is invoked with the connection, a
    literal passes through untouched."""
    resolved: list[Any] = []
    for arg in args:
        resolved.append(arg(conn) if callable(arg) else arg)
    return tuple(resolved)
