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

from sqlproof.exceptions import SqlProofGenerationError
from sqlproof.generators.typespec import TypeSpec, spec_for_column
from sqlproof.schema.model import Column

# Excludes \x00 (Postgres rejects it in text) and stays inside ASCII,
# which keeps COPY output compact. The Hypothesis path deliberately
# explores a much wider alphabet -- that is a search concern, not a
# volume concern, and the paths are not required to agree on values.
BULK_TEXT_ALPHABET = string.ascii_letters + string.digits + " _-."

DEFAULT_NULL_FRAC = 0.1

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
    return sampler_for_spec(TypeSpec(kind="text", max_size=255), rng)


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
