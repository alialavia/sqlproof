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
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlproof.exceptions import SqlProofGenerationError
from sqlproof.generators.narrowing import narrow_spec_for_checks

# These four are private to rows.py, but they are the exact functions
# that give the bulk path and the Hypothesis path identical primary
# keys, foreign-key resolution, and CHECK narrowing -- re-deriving them
# here would be the kind of drift this design exists to prevent, so we
# reach across the module boundary deliberately rather than duplicate.
from sqlproof.generators.rows import (
    _domain_checks_as_column_checks,  # pyright: ignore[reportPrivateUsage]
    _foreign_key_for_column,  # pyright: ignore[reportPrivateUsage]
    _is_single_column_unique,  # pyright: ignore[reportPrivateUsage]
    _unique_value,  # pyright: ignore[reportPrivateUsage]
)
from sqlproof.generators.typespec import TypeSpec, spec_for_column
from sqlproof.schema.model import Column, Table

# Excludes \x00 (Postgres rejects it in text) and stays inside ASCII,
# which keeps COPY output compact. The Hypothesis path deliberately
# explores a much wider alphabet -- that is a search concern, not a
# volume concern, and the paths are not required to agree on values.
BULK_TEXT_ALPHABET = string.ascii_letters + string.digits + " _-."

# Tuned to match the Hypothesis path's observed null_frac for nullable
# columns (st.one_of(st.none(), ...) in columns.py), as measured by
# tests/integration/test_pg_stats_parity_live.py against live pg_stats.
#
# Measured (a bare `text` column, no CHECK/unique/FK, N=800 rows,
# `draw_example(dataset_strategy(...))` -> `_insert_dataset` -> ANALYZE
# -> pg_stats.null_frac, repeated across many independent runs):
# 0.9988, 0.9988, 1.0, 0.9988, 1.0, 0.9988, 1.0, 1.0, 0.99875 -- stable
# at ~0.999-1.0, not noise.
#
# This is NOT "one_of is 50/50" -- it is a real, reproducible property
# of the *specific* mechanism `_insert_dataset`'s callers use to draw a
# dataset: `draw_example` (sampling.py) is `SearchStrategy.example()`,
# which internally runs `@given(strategy)` with `phases=(Phase.generate,)`
# and `max_examples=10` only, then shuffles and returns one of those 10
# full datasets. With no `Phase.reuse`/`Phase.target`/`Phase.shrink` and
# only 10 examples, Hypothesis never leaves the "cold start" region of
# its search, where `one_of(st.none(), strategy)` is heavily biased
# toward its first, simplest branch (`none()`). This is the same
# mechanism `SqlProof.invariant()` uses (core.py calls `draw_example`
# directly), so it is a real characteristic of that entry point, not a
# test artifact.
#
# It is emphatically NOT how `SqlProof.check()` behaves: that path
# (runners/property.py) drives the same strategy through a proper
# `@given(strategy) @settings(max_examples=runs)` loop with default
# phases, which explores far more of the search space and drives the
# null rate toward the *opposite* extreme (~0.2% null, measured
# separately over 500 examples with `phases=(Phase.generate,)` and no
# batch-of-10 cutoff). The Hypothesis path's null rate is not a single
# number -- it depends entirely on which SqlProof entry point drew the
# data. This constant matches `_insert_dataset`/`draw_example`, which is
# what this parity test (and `invariant()`) actually exercise.
DEFAULT_NULL_FRAC = 1.0

# Bound for the range sampler's distinct-pair retry loop (see
# sampler_for_spec's "range" branch). A range over a base type whose
# element domain has only one possible value (e.g. a single-value
# enum) can never produce two distinct draws, so the loop must give up
# loudly instead of spinning forever.
_RANGE_RETRY_LIMIT = 100

_EPOCH_DATE = date(2000, 1, 1)


def sampler_for_spec(spec: TypeSpec, rng: random.Random) -> Callable[[], Any]:
    kind = spec.kind
    if kind == "integer":
        # An integer spec's bounds are always plain `int` -- only a
        # decimal spec's can be `Decimal` (see typespec.py) -- so this
        # cast is just narrowing the field's static type, not a
        # runtime conversion of anything unusual.
        lo = int(spec.min_value or 0)
        hi = int(spec.max_value or 0)
        return lambda: rng.randint(lo, hi)
    if kind == "decimal":
        # `min_value`/`max_value` may now be `Decimal` -- narrowing.py
        # represents an exclusive bound (`rate > 1`) as one unit in
        # the last place off the literal, e.g. `Decimal("1.0001")`,
        # which an `int` can't hold. `random.uniform` needs floats
        # (Decimal doesn't support arithmetic with the float `random()`
        # returns), so convert once here -- but float(Decimal(...)) can
        # round a hair outside the original bound, and the whole point
        # of an exclusive bound is that it's respected exactly. Keep
        # the precise Decimal bounds too, and clamp each quantised
        # draw back into them.
        lo_dec = Decimal(spec.min_value or 0)
        hi_dec = Decimal(spec.max_value or 0)
        lo_f = float(lo_dec)
        hi_f = float(hi_dec)
        places = spec.places if spec.places is not None else 2
        quant = Decimal(1).scaleb(-places)

        def draw_decimal() -> Decimal:
            value = Decimal(rng.uniform(lo_f, hi_f)).quantize(quant)
            return min(max(value, lo_dec), hi_dec)

        return draw_decimal
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
        # TypeSpec has no representation for the recursive JSON shape
        # (unlike range/composite, which nest via `element`/`fields`);
        # this is a known, accepted leak in the registry design, so
        # the recursion is hard-coded here rather than read from spec,
        # matching columns.py's same accepted leak.
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
        element_spec = spec.element
        element = sampler_for_spec(element_spec, rng)

        def draw_range() -> Any:
            from psycopg.types.range import Range

            a, b = element(), element()
            attempts = 1
            while a == b:
                if attempts >= _RANGE_RETRY_LIMIT:
                    msg = (
                        "Cannot generate a non-empty range: its element type "
                        f"({_describe_spec(element_spec)}) has too small a "
                        f"domain to draw two distinct values after "
                        f"{_RANGE_RETRY_LIMIT} attempts."
                    )
                    raise SqlProofGenerationError(msg)
                b = element()
                attempts += 1
            return Range(min(a, b), max(a, b), "[)")

        return draw_range
    if kind == "composite":
        field_samplers = tuple(
            (name, sampler_for_spec(sub, rng)) for name, sub in spec.fields
        )
        return lambda: {name: fn() for name, fn in field_samplers}
    raise SqlProofGenerationError(
        f"sampler_for_spec: unhandled SpecKind {kind!r}; "
        "ensure typespec.py's KNOWN_TYPE_NAMES has a handler in this interpreter"
    )


def sampler_for_column(
    column: Column,
    rng: random.Random,
    *,
    null_frac: float = DEFAULT_NULL_FRAC,
    spec: TypeSpec | None = None,
) -> Callable[[], Any]:
    if spec is None:
        spec, nullable = spec_for_column(column)
    else:
        nullable = column.nullable
    base = sampler_for_spec(spec, rng)
    if not nullable:
        return base
    return lambda: None if rng.random() < null_frac else base()


def _describe_spec(spec: TypeSpec) -> str:
    # TypeSpec carries no source type name (e.g. the concrete Postgres
    # range/enum name), so this describes the element's *shape*
    # instead -- the actionable detail for a "domain too small"
    # diagnostic. Enum gets its declared values spelled out since
    # that's the common way a range ends up with a degenerate domain.
    if spec.kind == "enum":
        return f"enum{spec.enum_values!r}"
    return spec.kind


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
        # Bounded Zipf via Pareto: resample until inside range. Verified
        # empirically at parent_count=100, alpha=1.2 -- top-5 parents take
        # 49.9% of children (uniform would be ~5%) with all 100 parents
        # still reachable. Retry exhaustion does not occur in practice;
        # the uniform fallback exists only so the function is total.
        for _ in range(100):
            candidate = int(rng.paretovariate(zipf_alpha - 1))
            if 1 <= candidate <= parent_count:
                return candidate - 1
        return rng.randrange(parent_count)
    return rng.randrange(parent_count)


def composite_key_values(
    table: Table,
    index: int,
    parent_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Assign the `index`-th distinct composite key for `table`.

    Mixed-radix decomposition of the row index. Where a key column is
    also a foreign key, its radix is the parent's row count so the value
    is always a live parent key; a key column that is not a foreign key
    has no natural radix, so it is unbounded and simply counts.

    Collision-free by construction, so no seen-set and no retry loop --
    which is what keeps this O(1) per row.

    Bounded (FK-backed) columns are assigned their digit BEFORE the one
    unbounded column, regardless of declaration order in the primary
    key. This must be independent of declaration order: an unbounded
    column always absorbs whatever remains, so if it were processed
    before a bounded sibling, that sibling would be left with `0 %
    radix == 0` on every row -- pinned to a single parent forever
    (e.g. `PRIMARY KEY (org_id, seq)` with `org_id` a foreign key and
    `seq` a plain counter: every row would get the same `org_id`).
    Processing bounded columns first, unconditionally, is what keeps
    the assignment correct no matter which order the columns were
    declared in.
    """
    parent_counts = parent_counts or {}
    fk_names: list[str] = []
    unbounded_names: list[str] = []
    for name in table.primary_key:
        if _foreign_key_for_column(table, name) is not None:
            fk_names.append(name)
        else:
            unbounded_names.append(name)
    if len(unbounded_names) > 1:
        msg = (
            f"Cannot assign a composite primary key for {table.name}: "
            f"columns {tuple(unbounded_names)} of primary key "
            f"{table.primary_key} are not foreign keys, so none has a "
            f"natural radix. At most one non-foreign-key column is "
            f"supported in a composite primary key."
        )
        raise SqlProofGenerationError(msg)

    remaining = index
    values: dict[str, Any] = {}
    # Order among the bounded columns themselves doesn't affect
    # correctness (any consistent order stays collision-free) -- only
    # last-vs-not-last relative to the unbounded column does.
    for name in reversed(fk_names):
        column = table.column(name)
        fk = _foreign_key_for_column(table, name)
        assert fk is not None  # fk_names is exactly the FK-backed columns
        radix = parent_counts.get(fk.referenced_table, 0)
        if radix > 0:
            position = remaining % radix
            remaining //= radix
        else:
            position = remaining
            remaining = 0
        values[name] = _unique_value(name, column.type.name, position)
    for name in unbounded_names:
        column = table.column(name)
        values[name] = _unique_value(name, column.type.name, remaining)
        remaining = 0
    return values


def _composite_key_space(table: Table, parent_counts: Mapping[str, int]) -> int | None:
    """Number of distinct composite keys available, or None if unbounded."""
    space = 1
    for name in table.primary_key:
        fk = _foreign_key_for_column(table, name)
        if fk is None:
            return None  # a non-FK key column can count without limit
        radix = parent_counts.get(fk.referenced_table, 0)
        if radix <= 0:
            return 0
        space *= radix
    return space


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

    Composite primary keys are assigned the same way, via
    `composite_key_values`: a mixed-radix decomposition of the row
    index rather than a single `_unique_value` call, so no seen-set is
    needed to keep them collision-free.
    """
    composite_pk = len(table.primary_key) > 1
    if composite_pk:
        space = _composite_key_space(table, parent_counts)
        if space is not None and space < count:
            msg = (
                f"Cannot generate {count} rows for {table.name}: only {space} "
                f"distinct composite keys exist for primary key "
                f"{table.primary_key} at the given parent sizes."
            )
            raise SqlProofGenerationError(msg)

    single_pk = table.primary_key[0] if len(table.primary_key) == 1 else None
    samplers: dict[str, Callable[[], Any]] = {}
    for column in table.columns:
        if column.name in table.primary_key or column.is_generated:
            continue
        if column.default is not None:
            continue
        if _foreign_key_for_column(table, column.name) is not None:
            continue
        if _is_single_column_unique(table, column.name):
            continue
        # Narrow the spec by this column's CHECK constraints (and any
        # inherited from a domain) BEFORE building the sampler, so the
        # values are in-range by construction rather than by rejection.
        # sampler_for_column already handles nullability -- passing it
        # the narrowed spec (rather than re-deriving null-wrapping here)
        # keeps null handling in exactly one place.
        spec, _nullable = spec_for_column(column)
        spec = narrow_spec_for_checks(
            spec,
            column,
            table.check_constraints + _domain_checks_as_column_checks(column),
        )
        samplers[column.name] = sampler_for_column(
            column, rng, null_frac=null_frac, spec=spec
        )

    for index in range(count):
        row: dict[str, Any] = {}
        key_values = composite_key_values(table, index, parent_counts) if composite_pk else {}
        for column in table.columns:
            name = column.name
            if name in key_values:
                row[name] = key_values[name]
                continue
            if name == single_pk:
                row[name] = _unique_value(name, column.type.name, index)
                continue
            if column.is_generated or column.default is not None:
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
