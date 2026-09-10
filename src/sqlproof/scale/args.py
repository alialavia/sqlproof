"""Resolving the arguments a probed function is called with.

Re-run at every scale point, against the data just loaded. That is
required rather than tidy: the dataset is regenerated at each factor and
keys are assigned deterministically (`_unique_value` gives id = i + 1),
so a literal that exists at 8x may not exist at 1x.

What each built-in resolver picks -- plainly, since none of them is a
worst-case guarantee:

- `heaviest`: the LARGEST KEY VALUE. The sweep loads the bulk
  generator's default uniform distribution, so that is an arbitrary
  parent, not the most-referenced one (under zipf, the most-referenced
  parent would be key 1, the smallest key).
- `median_key`: the middle key in key order.
- `random_key`: the key whose hash with `seed` sorts first.

The question this feature asks is "will this fall over?", and the case to
measure for that is the most-referenced parent under realistic skew.
Nothing here picks it yet: that needs a resolver that counts referencing
rows and a sweep that loads skewed data, both deliberately deferred. So
the run artifact records, per argument, which resolver chose it
(`argument_policy`), and never labels a run "worst case".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, cast

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


def _mark(resolve: Resolver, **policy: Any) -> Resolver:
    """Attach the marker `argument_policy` reads: how `resolve` picks its
    value, recorded per argument in the run artifact."""
    resolve._sqlproof_resolver = dict(policy)  # type: ignore[attr-defined]
    return resolve


def heaviest(column: str) -> Resolver:
    """The LARGEST KEY VALUE in `column` (`"table.column"`, optionally
    schema-qualified), re-resolved against each freshly loaded dataset.

    Not the most-referenced parent, and no worst-case guarantee: it
    orders by the key itself and never counts referencing rows. The
    sweep loads the bulk generator's default UNIFORM distribution, under
    which children spread evenly across parents, so the largest key is an
    arbitrary parent. Under zipf skew it would be the opposite of the
    worst case: the most-referenced parent is then key 1, the SMALLEST
    key. Counting referencing rows -- and loading skewed data for that to
    matter -- is future work; meanwhile the artifact records this
    argument as `{"kind": "heaviest", ...}`, never as a worst case.
    """
    table, name = _split(column)

    def resolve(conn: Any) -> Any:
        # Identifiers come from the caller's schema (table/column names),
        # never from row data, so this interpolation is not user input.
        sql = f"SELECT {name} FROM {table} ORDER BY {name} DESC LIMIT 1"
        return _scalar(conn, sql, column)

    return _mark(resolve, kind="heaviest", column=column)


def random_key(column: str, *, seed: int = 0) -> Resolver:
    """An arbitrary key from `column`: the one whose `md5(key || seed)`
    sorts first, re-resolved against each freshly loaded dataset.

    Deterministic for a given `seed` and dataset, so a run can be
    repeated; another `seed` picks another, equally arbitrary key.
    `seed` is coerced with `int()` before it is interpolated into the
    query, so nothing but an integer reaches the SQL.
    """
    table, name = _split(column)
    seed = int(seed)

    def resolve(conn: Any) -> Any:
        # Identifiers come from the caller's schema; see `heaviest` above.
        sql = (
            f"SELECT {name} FROM {table} "
            f"ORDER BY md5({name}::text || '{seed}') LIMIT 1"
        )
        return _scalar(conn, sql, column)

    return _mark(resolve, kind="random_key", column=column, seed=seed)


def median_key(column: str) -> Resolver:
    """The middle key of `column` in key order (`OFFSET count(*) / 2`),
    re-resolved against each freshly loaded dataset: a typical key, not
    an extreme one."""
    table, name = _split(column)

    def resolve(conn: Any) -> Any:
        # Identifiers come from the caller's schema; see `heaviest` above.
        sql = (
            f"SELECT {name} FROM {table} ORDER BY {name} "
            f"OFFSET (SELECT count(*) / 2 FROM {table}) LIMIT 1"
        )
        return _scalar(conn, sql, column)

    return _mark(resolve, kind="median_key", column=column)


def resolve_args(conn: Any, args: Sequence[Any]) -> tuple[Any, ...]:
    """Resolve each entry: a callable is invoked with the connection, a
    literal passes through untouched."""
    resolved: list[Any] = []
    for arg in args:
        resolved.append(arg(conn) if callable(arg) else arg)
    return tuple(resolved)


def argument_policy(args: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    """How each argument position is chosen, one entry per position, for
    the run artifact (Ruling AO) -- so an exponent is never read as
    describing a case its arguments did not pick:

    - `{"kind": "heaviest", "column": ...}`, `{"kind": "median_key",
      "column": ...}` or `{"kind": "random_key", "column": ..., "seed":
      ...}` for a built-in resolver, read from its marker;
    - `{"kind": "callable"}` for any other callable, which sqlproof
      cannot describe;
    - `{"kind": "literal"}` for a value passed through as given (the
      value itself is recorded in every point's `args`).
    """
    policy: list[dict[str, Any]] = []
    for arg in args:
        marker = getattr(arg, "_sqlproof_resolver", None)
        if isinstance(marker, dict):
            policy.append(dict(cast("dict[str, Any]", marker)))
        elif callable(arg):
            policy.append({"kind": "callable"})
        else:
            policy.append({"kind": "literal"})
    return tuple(policy)
