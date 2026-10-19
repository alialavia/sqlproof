"""Declarative type knowledge, consumed by both generation paths.

This module holds what Postgres itself *constrains* about a type:
`int4` really is ±2³¹, `numeric(6, 2)` really does cap at 9999.99,
`varchar(n)` really does stop at n. It contains no Hypothesis
strategies and no RNG. `columns.py` interprets a TypeSpec into a
Hypothesis strategy; `bulk.py` interprets the same TypeSpec into a
seeded sampler.

Six kinds -- `date`, `datetime`, `float`, `interval`, `binary` and
`vector` -- have no meaningful bound in Postgres itself (dates run from
4713 BC to 5874897 AD). For those, this module also holds the one
*window* both generators draw from: the `*_WINDOW` constants, read
through `float_bounds` and the window helpers below. Before they were
shared, each interpreter hard-coded its own, and they diverged by orders
of magnitude (dates 2000-2054 in one, 0001-9999 in the other), so a
Hypothesis-drawn dataset and a bulk-loaded one described different
data. A scenario's `strategy()` and `load()` must agree, and so must a
property test and the scale sweep built on the same schema, so the
windows are now facts here rather than policy in each interpreter.

The windows are the bulk generator's: realistic, so planner statistics
over bulk data mean something. The cost is that Hypothesis no longer
explores year 1 or year 9999 by default. Callers who want extremes ask
for them per column -- `sqlproof(..., columns={...})` on the Hypothesis
path, `load_dataset(..., columns={...})` on the bulk path.

Adding a new type means adding one entry here plus a branch in each
interpreter; `tests/unit/test_interpreter_exhaustiveness.py` fails if
either branch is missing, and `tests/unit/test_shared_windows.py` fails
if the two interpreters draw outside a shared window.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import Any, Literal

from sqlproof.exceptions import SqlProofSchemaError
from sqlproof.schema.model import Column, PgType

SpecKind = Literal[
    "integer", "decimal", "float", "boolean", "text", "uuid",
    "datetime", "date", "time", "interval", "json", "binary",
    "vector", "enum", "range", "composite", "array",
]


DATE_WINDOW = (date(2000, 1, 1), date(2000, 1, 1) + timedelta(days=20_000))
"""Dates both generators draw from: 2000-01-01 to 2054-10-04."""
DATETIME_WINDOW = (datetime(2000, 1, 1), datetime(2000, 1, 1) + timedelta(seconds=10**9))
"""Timestamps both generators draw from, to the second: 2000-01-01 to
2031-09-09. Naive; a tz-aware spec uses the same instants in UTC."""
INTERVAL_WINDOW = (timedelta(0), timedelta(seconds=10**7))
"""Intervals both generators draw from: zero to about 115 days."""
FLOAT_WINDOW = (-1e6, 1e6)
"""Floats both generators draw from when no CHECK bounds the column."""
VECTOR_COMPONENT_WINDOW = (-1.0, 1.0)
"""Each component of a generated pgvector value."""
BINARY_MAX_SIZE = 32
"""The longest generated `bytea` value, in bytes."""
ARRAY_MAX_SIZE = 5
"""The most elements a generated array value holds (one dimension), unless
a CHECK (`cardinality(v) >= 8`) asks for more."""


@dataclass(frozen=True, slots=True)
class TypeSpec:
    kind: SpecKind
    # Plain `int` for an integer spec. A decimal spec's bounds are
    # `int` too until narrowed by an exclusive CHECK (`rate > 1`) --
    # narrowing.py then needs to represent a bound that sits one unit
    # in the last place off a literal (e.g. `Decimal("1.0001")` at
    # scale 4), which an `int` field cannot hold without rounding away
    # the whole point of the narrowing. Both interpreters already
    # accept a `Decimal` here; decimal_unit_bounds converts exact
    # bounds to integral units of the declared scale for sampling.
    # A float spec has no bounds unless a CHECK narrows it; narrowing
    # then stores the exact Decimal value of a float8 bound.
    min_value: int | Decimal | None = None
    max_value: int | Decimal | None = None
    places: int | None = None
    min_size: int | None = None
    max_size: int | None = None
    float_width: int | None = None
    # A genuine Postgres enum's labels are always `str`. A CHECK-
    # narrowed IN-list/ANY(ARRAY[...]) on a non-text column carries
    # values coerced to that column's own kind instead (`int` for an
    # integer column, `Decimal` for a decimal one) -- both interpreters
    # hand these to a sampler with no further coercion, so the type
    # here has to already match what the target column accepts.
    enum_values: tuple[Any, ...] = ()
    # Values a CHECK forbids (`status <> 'deleted'`, `code NOT IN
    # (...)`) that no other field can express. Both interpreters
    # reject draws equal to one of these. Coerced to the spec's kind
    # by narrowing.py, like `enum_values`.
    excluded_values: tuple[Any, ...] = ()
    dimension: int | None = None
    element: TypeSpec | None = None
    fields: tuple[tuple[str, TypeSpec], ...] = ()
    tz_aware: bool = False


def _int(lo: int, hi: int) -> Callable[[PgType], TypeSpec]:
    return lambda _t: TypeSpec(kind="integer", min_value=lo, max_value=hi)


def _numeric(t: PgType) -> TypeSpec:
    # `numeric(p, s)` admits values with at most `p - s` integer digits, so
    # the largest legal magnitude is 10^(p-s) minus one unit in the last
    # place. Without this cap the generator happily draws values Postgres
    # rejects with "numeric field overflow" — see #85 / #91.
    #
    # A bare `numeric` declares no precision, so there is nothing to cap
    # against and we fall back to a broad-but-bounded range.
    if t.modifiers:
        precision = t.modifiers[0]
        scale = t.modifiers[1] if len(t.modifiers) > 1 else 0
        # Tuple construction is exact even beyond the caller's Decimal
        # context precision. Arithmetic here could round 999...99 to
        # 1000...00, admitting a value PostgreSQL rejects.
        max_abs = Decimal((0, (9,) * precision, -scale))
        return TypeSpec(
            kind="decimal", min_value=max_abs.copy_negate(), max_value=max_abs, places=scale
        )
    return TypeSpec(kind="decimal", min_value=-1_000_000, max_value=1_000_000, places=2)


def decimal_unit_bounds(spec: TypeSpec) -> tuple[int, int]:
    """Inclusive integer coefficients at the spec's scale, rounded inward."""
    lo, hi = Decimal(spec.min_value or 0), Decimal(spec.max_value or 0)
    places = spec.places if spec.places is not None else 2
    with localcontext() as context:
        context.prec = max(len(lo.as_tuple().digits), len(hi.as_tuple().digits))
        low = int(lo.scaleb(places).to_integral_value(rounding=ROUND_CEILING))
        high = int(hi.scaleb(places).to_integral_value(rounding=ROUND_FLOOR))
    return low, high


def decimal_from_units(units: int, places: int) -> Decimal:
    """Apply a decimal scale without rounding through float or Decimal context."""
    sign, digits, _ = Decimal(units).as_tuple()
    return Decimal((sign, digits, -places))


def _varchar(t: PgType) -> TypeSpec:
    return TypeSpec(kind="text", max_size=t.modifiers[0] if t.modifiers else 255)


def _char(t: PgType) -> TypeSpec:
    size = t.modifiers[0] if t.modifiers else 1
    return TypeSpec(kind="text", min_size=size, max_size=size)


def _vector(t: PgType) -> TypeSpec:
    if not t.modifiers:
        msg = (
            "vector type requires a dimension (e.g. vector(384)); "
            "got vector with no modifier"
        )
        raise SqlProofSchemaError(msg)
    return TypeSpec(kind="vector", dimension=t.modifiers[0])


def _timestamp(_t: PgType) -> TypeSpec:
    # Plain timestamptz columns use naive datetimes, matching the
    # existing behaviour in columns.py. Only tstzrange elements need
    # tz-aware values for PyGreSQL's adapter to type them correctly.
    return TypeSpec(kind="datetime", tz_aware=False)


TYPE_SPEC_BUILDERS: dict[str, Callable[[PgType], TypeSpec]] = {
    "smallint": _int(-32_768, 32_767),
    "int2": _int(-32_768, 32_767),
    "integer": _int(-2_147_483_648, 2_147_483_647),
    "int": _int(-2_147_483_648, 2_147_483_647),
    "int4": _int(-2_147_483_648, 2_147_483_647),
    "serial": _int(-2_147_483_648, 2_147_483_647),
    "bigint": _int(-(2**63), 2**63 - 1),
    "int8": _int(-(2**63), 2**63 - 1),
    "bigserial": _int(-(2**63), 2**63 - 1),
    "numeric": _numeric,
    "decimal": _numeric,
    "real": lambda _t: TypeSpec(kind="float", float_width=32),
    "float4": lambda _t: TypeSpec(kind="float", float_width=32),
    "double precision": lambda _t: TypeSpec(kind="float"),
    "float8": lambda _t: TypeSpec(kind="float"),
    "boolean": lambda _t: TypeSpec(kind="boolean"),
    "bool": lambda _t: TypeSpec(kind="boolean"),
    "text": lambda _t: TypeSpec(kind="text", max_size=255),
    "citext": lambda _t: TypeSpec(kind="text", max_size=255),
    "varchar": _varchar,
    "character varying": _varchar,
    "char": _char,
    "character": _char,
    # `bpchar` is the internal name Postgres reports for `char(n)` through
    # introspection, so it must map to the same fixed-width builder.
    "bpchar": _char,
    "uuid": lambda _t: TypeSpec(kind="uuid"),
    "timestamp": _timestamp,
    "timestamp without time zone": _timestamp,
    "timestamptz": _timestamp,
    "timestamp with time zone": _timestamp,
    "date": lambda _t: TypeSpec(kind="date"),
    "time": lambda _t: TypeSpec(kind="time"),
    "timetz": lambda _t: TypeSpec(kind="time"),
    "interval": lambda _t: TypeSpec(kind="interval"),
    "json": lambda _t: TypeSpec(kind="json"),
    "jsonb": lambda _t: TypeSpec(kind="json"),
    "bytea": lambda _t: TypeSpec(kind="binary", min_size=0, max_size=BINARY_MAX_SIZE),
    "vector": _vector,
}

_FLOAT32_MAX = 3.4028234663852886e38


def spec_is_empty(spec: TypeSpec) -> bool:
    """Whether CHECK narrowing left `spec` with no admissible value
    (`length(x) >= 100` on a varchar(5), `n > 10 AND n < 5`, an
    IN-list disjoint from an enum's labels)."""
    if spec.kind == "enum":
        return not spec.enum_values
    if (
        spec.min_value is not None
        and spec.max_value is not None
        and Decimal(spec.min_value) > Decimal(spec.max_value)
    ):
        return True
    if spec.kind == "decimal" and spec.places is not None:
        low, high = decimal_unit_bounds(spec)
        if low > high:
            return True
    if spec.min_size is not None and spec.max_size is not None:
        return spec.min_size > spec.max_size
    return False


def _to_float32(value: float) -> float:
    value = max(min(value, _FLOAT32_MAX), -_FLOAT32_MAX)
    return float(struct.unpack("<f", struct.pack("<f", value))[0])


def _float32_step(value: float, *, up: bool) -> float:
    if value == 0.0:
        bits = 1
        smallest = float(struct.unpack("<f", struct.pack("<I", bits))[0])
        return smallest if up else -smallest
    bits = struct.unpack("<I", struct.pack("<f", value))[0]
    bits = bits + 1 if (value > 0) == up else bits - 1
    return float(struct.unpack("<f", struct.pack("<I", bits))[0])


def float32_ceil(value: float) -> float:
    """Smallest float32-representable value >= `value`.

    A `real` column stores float32, so a CHECK bound on one has to be
    honored after that rounding; both interpreters use this (and
    `float32_floor`) to keep narrowed `real` draws inside the bound.
    """
    rounded = _to_float32(value)
    return rounded if rounded >= value else _float32_step(rounded, up=True)


def float32_floor(value: float) -> float:
    """Largest float32-representable value <= `value`."""
    rounded = _to_float32(value)
    return rounded if rounded <= value else _float32_step(rounded, up=False)


def float_bounds(spec: TypeSpec) -> tuple[float, float]:
    """The closed range a float spec draws from, in both generators.

    Unbounded floats stay in `FLOAT_WINDOW`. A CHECK bound replaces the
    matching side of the window; a one-sided bound beyond the window's
    far edge gets a window of its own width. A `real` column's range is
    rounded inward to float32, which Hypothesis requires and Postgres
    stores."""
    lo = float(spec.min_value) if spec.min_value is not None else None
    hi = float(spec.max_value) if spec.max_value is not None else None
    if spec.float_width == 32:
        lo = None if lo is None else float32_ceil(lo)
        hi = None if hi is None else float32_floor(hi)
    window_lo, window_hi = FLOAT_WINDOW
    width = window_hi - window_lo
    low = lo if lo is not None else (min(window_lo, hi - width) if hi is not None else window_lo)
    high = hi if hi is not None else (max(window_hi, low + width) if lo is not None else window_hi)
    if spec.float_width == 32:
        # A window edge derived from a bound may not be a float32 value.
        low, high = float32_ceil(low), float32_floor(high)
    return low, high


def array_size_bounds(spec: TypeSpec) -> tuple[int, int]:
    """The closed range of element counts an array spec draws, in both
    generators: 0 to `ARRAY_MAX_SIZE`, narrowed by a cardinality CHECK. A
    lower bound past the window with no upper bound draws exactly it."""
    low = spec.min_size or 0
    high = spec.max_size if spec.max_size is not None else max(low, ARRAY_MAX_SIZE)
    return low, high


KNOWN_TYPE_NAMES: frozenset[str] = frozenset(TYPE_SPEC_BUILDERS)

_FALLBACK = TypeSpec(kind="text", max_size=255)


def spec_for_type(pg_type: PgType) -> TypeSpec:
    if pg_type.kind == "enum":
        return TypeSpec(kind="enum", enum_values=pg_type.enum_values)
    if pg_type.kind == "domain" and pg_type.base is not None:
        # A domain is an alias plus optional CHECKs. The CHECKs are
        # enforced downstream in rows.py's refinement pipeline, which
        # knows the column name to substitute for VALUE.
        return spec_for_type(pg_type.base)
    if pg_type.kind == "array" and pg_type.base is not None:
        # The element's own domain CHECKs (`pos[]` with `pos` a domain
        # over int) are folded in by narrowing.py, like a scalar domain's.
        # No max_size: `array_size_bounds` applies the window, so a CHECK
        # can still ask for more elements than it holds.
        return TypeSpec(kind="array", element=spec_for_type(pg_type.base), min_size=0)
    if pg_type.kind == "range" and pg_type.base is not None:
        element = spec_for_type(pg_type.base)
        if pg_type.name == "tstzrange":
            element = TypeSpec(kind="datetime", tz_aware=True)
        return TypeSpec(kind="range", element=element)
    if pg_type.kind == "composite":
        return TypeSpec(
            kind="composite",
            fields=tuple((n, spec_for_type(t)) for n, t in pg_type.composite_fields),
        )
    builder = TYPE_SPEC_BUILDERS.get(pg_type.name.lower())
    if builder is None:
        return _FALLBACK
    return builder(pg_type)


def spec_for_column(column: Column) -> tuple[TypeSpec, bool]:
    return spec_for_type(column.type), column.nullable
