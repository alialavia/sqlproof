"""Resolving the arguments a probed function is called with.

Re-run at every scale point, against the data just loaded. That is
required rather than tidy: the dataset is regenerated at each factor and
keys are assigned deterministically (`_unique_value` gives id = i + 1),
so a literal that exists at 8x may not exist at 1x.

What each built-in resolver picks -- plainly, since none of them is a
worst-case guarantee:

- `heaviest`: the MOST-REFERENCED KEY: the one with the most rows
  pointing at it through single-column foreign keys, counted over every
  table that references the column. Ties, and a column nothing
  references, fall back to the largest key value.
- `median_key`: the middle key in key order.
- `random_key`: the key whose hash with `seed` sorts first.

The question this feature asks is "will this fall over?", and the case to
measure for that is the most-referenced parent under realistic skew:
`heaviest` with `distribution="zipf"`. Even that is the heaviest parent
by row count, not by cost, so the run artifact records, per argument,
which resolver chose it (`argument_policy`), and never labels a run
"worst case".
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


_REFERENCING = """
SELECT format('%%I.%%I', n.nspname, child.relname), quote_ident(a.attname)
FROM pg_constraint c
JOIN pg_class child ON child.oid = c.conrelid
JOIN pg_namespace n ON n.oid = child.relnamespace
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
JOIN pg_attribute p ON p.attrelid = c.confrelid AND p.attnum = c.confkey[1]
WHERE c.contype = 'f'
  AND cardinality(c.conkey) = 1
  AND c.confrelid = %s::regclass
  AND p.attname = %s
ORDER BY 1, 2
"""


def heaviest(column: str) -> Resolver:
    """The MOST-REFERENCED key in `column` (`"table.column"`, optionally
    schema-qualified), re-resolved against each freshly loaded dataset.

    Counts the rows pointing at each key through every single-column
    foreign key into `column`, from any table, and picks the key with
    the most; ties go to the largest key, and so does a column nothing
    references (the earlier behaviour). Under the sweep's default
    uniform distribution that is whichever parent drew the most
    children; with `distribution="zipf"` it is the parent that owns a
    disproportionate share -- the realistic heavy tenant. Heaviest by
    row count, not by what the function does with them, so the artifact
    records `{"kind": "heaviest", "by": "references"}`, never a worst
    case.
    """
    table, name = _split(column)

    def resolve(conn: Any) -> Any:
        children = conn.execute(_REFERENCING, [table, name]).fetchall()
        if not children:
            # Identifiers come from the caller's schema (table/column
            # names), never from row data, so this is not user input.
            sql = f"SELECT {name} FROM {table} ORDER BY {name} DESC LIMIT 1"
            return _scalar(conn, sql, column)
        # Child names come from the catalog, quoted there.
        counts = " UNION ALL ".join(
            f"SELECT {fk} AS k, count(*) AS n FROM {child} WHERE {fk} IS NOT NULL GROUP BY {fk}"
            for child, fk in children
        )
        sql = (
            f"SELECT t.{name} FROM {table} t "
            f"LEFT JOIN (SELECT k, sum(n) AS n FROM ({counts}) c GROUP BY k) r "
            f"ON r.k = t.{name} "
            f"ORDER BY coalesce(r.n, 0) DESC, t.{name} DESC LIMIT 1"
        )
        return _scalar(conn, sql, column)

    return _mark(resolve, kind="heaviest", column=column, by="references")


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
    - `{"kind": "ref", "name": ...}` for a scenario's named row;
    - `{"kind": "literal"}` for a value passed through as given (the
      value itself is recorded in every point's `args`).
    """
    from sqlproof.scenarios.model import RowRef

    policy: list[dict[str, Any]] = []
    for arg in args:
        marker = getattr(arg, "_sqlproof_resolver", None)
        if isinstance(arg, RowRef):
            policy.append({"kind": "ref", "name": arg.name})
        elif isinstance(marker, dict):
            policy.append(dict(cast("dict[str, Any]", marker)))
        elif callable(arg):
            policy.append({"kind": "callable"})
        else:
            policy.append({"kind": "literal"})
    return tuple(policy)
