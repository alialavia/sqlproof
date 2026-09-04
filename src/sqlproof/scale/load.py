"""COPY-based bulk loading: stream `bulk_table_rows` into Postgres.

`bulk_table_rows` (generators/bulk.py) produces valid rows fast; this
module is what puts them in a real database, in FK-safe order, via
`COPY` -- the only Postgres write path that stays fast at the row
counts this exists to reach (see the design doc's Evidence: the
Hypothesis path is projected at ~3h for 500k rows).

Postgres is the validity oracle here, not this module: a constraint
violation surfaces as a `COPY` failure. We never assert this path
agrees with the Hypothesis path about what is valid -- they could be
identically wrong -- we assert each independently against a live
database.
"""

from __future__ import annotations

import itertools
import random
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, LiteralString, cast

import psycopg
from psycopg import sql
from psycopg.types.json import Json, Jsonb

# Cross-module reuse of core.py's JSON-column adaptation, deliberately,
# for the same reason bulk.py reaches into rows.py's private helpers
# (see that file's comment): re-deriving "which columns need Json/
# Jsonb wrapping" here would be exactly the kind of drift this design
# exists to prevent -- COPY and the parameterized-INSERT path must
# agree on it, and there is exactly one place that already knows.
# Only `_base_type_name` is reused directly, not `_adapt_insert_value`
# itself -- see `_column_wrap_kind`'s docstring for why.
from sqlproof.core import _base_type_name  # pyright: ignore[reportPrivateUsage]
from sqlproof.exceptions import SqlProofGenerationError
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
    """Stream `rows` into `table` via `COPY`. Returns rows written.

    `column_names` must match the keys each row dict actually
    carries (see `load_dataset`, which derives it from a real row
    rather than an independent guess). Every value is adapted before
    hitting the wire (see `_column_wrap_kind`/`_adapt_copy_value`
    below): `COPY`'s text protocol resolves most Python types
    (Decimal, str, bytes, datetime, a generic `Range`, ...) to the
    right wire format on its own, but a bare `dict` for a json/jsonb
    column is ambiguous -- psycopg raises `cannot adapt type 'dict'`
    unless it's wrapped in `Json`/`Jsonb`, exactly as the
    parameterized INSERT path (`_adapt_insert_value` in core.py)
    needs.

    A `GENERATED ALWAYS AS IDENTITY` column carrying an explicit
    value (as its primary key does, once it's `table.primary_key`)
    is NOT rejected by `COPY` the way plain `INSERT` rejects it --
    verified empirically against this project's test database. No
    `OVERRIDING SYSTEM VALUE` equivalent is needed or available for
    `COPY`; it isn't necessary.
    """
    qualified = f"{_quote(table.schema)}.{_quote(table.name)}"
    columns = ", ".join(_quote(name) for name in column_names)
    # Identifiers are already safely quoted by `_quote` above; the cast
    # to LiteralString only satisfies pyright strict's `Query` type for
    # `Cursor.copy`, matching the pattern in mutation/runner.py.
    copy_sql = sql.SQL(cast(LiteralString, f"COPY {qualified} ({columns}) FROM STDIN"))  # type: ignore[redundant-cast]
    # Table-invariant: which columns need Json/Jsonb wrapping depends
    # only on the column's type, never on the row. `_adapt_insert_value`
    # (core.py) resolves this via `table.column(name)` -- a linear scan
    # over `table.columns` -- plus a walk up the type's domain/base
    # chain, per VALUE per ROW. Resolved once per column here, before
    # the `COPY` loop, instead (same hoist as generators/bulk.py's
    # per-row FK/unique lookups).
    wrap_kinds = [_column_wrap_kind(table, name) for name in column_names]
    written = 0
    with conn.cursor() as cur, cur.copy(copy_sql) as copy:
        for row in rows:
            copy.write_row(
                tuple(
                    _adapt_copy_value(row.get(name), kind)
                    for name, kind in zip(column_names, wrap_kinds, strict=True)
                )
            )
            written += 1
    return written


def _column_wrap_kind(table: Table, column_name: str) -> str | None:
    """Whether `column_name` needs Json/Jsonb wrapping for `COPY`, or
    None. Mirrors `_adapt_insert_value` (core.py)'s type resolution
    exactly, but is called once per column instead of once per value
    -- see `copy_table`.
    """
    type_name = _base_type_name(table.column(column_name))
    if type_name in ("jsonb", "json"):
        return type_name
    return None


def _adapt_copy_value(value: Any, wrap_kind: str | None) -> object:
    """Apply the wrapping `_column_wrap_kind` determined for this
    column. A Python `None` must stay `None` -- a wire-protocol NULL
    -- not be wrapped in Jsonb/Json, which would serialise it as the
    *jsonb scalar* `null`: a real, non-NULL value for which `IS NULL`
    is false. Mirrors `_adapt_insert_value` (core.py) exactly.
    """
    if value is None:
        return None
    if wrap_kind == "jsonb":
        return Jsonb(value)
    if wrap_kind == "json":
        return Json(value)
    return value


def load_dataset(
    conn: psycopg.Connection,
    schema: SchemaInfo,
    sizes: Mapping[str, int],
    *,
    seed: int = 0,
    distribution: str = "uniform",
    null_frac: float = DEFAULT_NULL_FRAC,
) -> dict[str, int]:
    """Generate and `COPY` every table in FK-safe order.

    Reuses `resolve_insertion_plan` so parents always land before
    children -- the same ordering the Hypothesis path uses.
    """
    plan = resolve_insertion_plan(schema.tables)
    if plan.deferred_edges:
        # core.py:346-391 resolves FK cycles with a second UPDATE
        # pass. The bulk path has no equivalent yet. Without this
        # guard the load SUCCEEDS and every deferred FK is silently
        # NULL -- and a measurement taken against data whose
        # relationships do not exist is worse than no measurement,
        # because it looks correct.
        edges = ", ".join(
            f"{e.source_table}->{e.referenced_table}" for e in plan.deferred_edges
        )
        msg = (
            f"Bulk loading does not yet support foreign-key cycles "
            f"(deferred edges: {edges}). The Hypothesis path handles these "
            f"via a second UPDATE pass; until the bulk loader does the "
            f"same, loading would silently leave these columns NULL."
        )
        raise SqlProofGenerationError(msg)
    rng = random.Random(seed)
    loaded: dict[str, int] = {}
    for table in plan.ordered_tables:
        count = sizes.get(table.name, 0)
        if count <= 0:
            continue
        rows = bulk_table_rows(
            table,
            count=count,
            rng=rng,
            parent_counts=loaded,
            distribution=distribution,
            null_frac=null_frac,
        )
        # The column list is derived from a real row `bulk_table_rows`
        # produced, not re-filtered from `table.columns` independently.
        # A single-column primary key is assigned a value regardless
        # of `is_generated`/`default` (to match the Hypothesis path --
        # see generators/bulk.py), so "generated or has a default"
        # is no longer the same set of columns as "present in the
        # row". Two independent rules for "which columns" is exactly
        # the drift this codebase keeps getting bitten by; asking the
        # row itself is the only way to stay in sync by construction.
        first_row = next(rows)
        column_names = list(first_row.keys())
        all_rows = itertools.chain([first_row], rows)
        loaded[table.name] = copy_table(conn, table, all_rows, column_names)
    return loaded


def analyze(conn: psycopg.Connection, schema: SchemaInfo) -> None:
    """Refresh planner statistics.

    Non-optional after a bulk load: without it the planner has no
    statistics for the new rows and will choose plans that have
    nothing to do with what a user would see.
    """
    for table in schema.tables:
        qualified = f"{_quote(table.schema)}.{_quote(table.name)}"
        conn.execute(sql.SQL(cast(LiteralString, f"ANALYZE {qualified}")))  # type: ignore[redundant-cast]
