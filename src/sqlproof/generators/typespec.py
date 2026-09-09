"""Declarative type knowledge, consumed by both generation paths.

This module holds what Postgres itself *constrains* about a type:
`int4` really is ±2³¹, `numeric(6, 2)` really does cap at 9999.99,
`varchar(n)` really does stop at n. Facts, not preferences. It contains
no Hypothesis strategies and no RNG. `columns.py` interprets a TypeSpec
into a Hypothesis strategy; `bulk.py` interprets the same TypeSpec into
a seeded sampler.

What this module deliberately does NOT hold is *policy*. Several types
— `date`, `datetime`, `float`, `interval`, `binary` — have no
meaningful bound to record: Postgres accepts dates from 4713 BC to
5874897 AD, and neither generator wants that. Each interpreter picks a
range suited to its own job, and the two differ on purpose:

    columns.py  explores extremes, because its job is finding the input
                that breaks your code — `st.dates()` spans years 1-9999.
    bulk.py     stays realistic, because its job is producing data whose
                planner statistics mean something — dates land in a
                few-decade window.

Narrowing the search path would blunt it; widening the volume path
would make `n_distinct` and `correlation` describe nothing real. So the
divergence is intended, and neither range is "more correct".

Callers who need a specific range say so per column rather than
relying on either default — `sqlproof(..., columns={...})` on the
Hypothesis path, `load_dataset(..., columns={...})` on the bulk path.

Adding a new type means adding one entry here plus a branch in each
interpreter; `tests/unit/test_interpreter_exhaustiveness.py` fails if
either branch is missing. Note what that test does and does not buy:
it proves both interpreters *handle* every kind, never that they agree
on what a kind means. For kinds with facts recorded here, agreement is
structural. For the policy kinds above, it is not — by design.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from sqlproof.exceptions import SqlProofSchemaError
from sqlproof.schema.model import Column, PgType

SpecKind = Literal[
    "integer", "decimal", "float", "boolean", "text", "uuid",
    "datetime", "date", "time", "interval", "json", "binary",
    "vector", "enum", "range", "composite",
]


@dataclass(frozen=True, slots=True)
class TypeSpec:
    kind: SpecKind
    # Plain `int` for an integer spec. A decimal spec's bounds are
    # `int` too until narrowed by an exclusive CHECK (`rate > 1`) --
    # narrowing.py then needs to represent a bound that sits one unit
    # in the last place off a literal (e.g. `Decimal("1.0001")` at
    # scale 4), which an `int` field cannot hold without rounding away
    # the whole point of the narrowing. Both interpreters already
    # accept a `Decimal` here (columns.py wraps in `Decimal(...)`
    # regardless; bulk.py converts to `float` for `random.uniform`).
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
    if len(t.modifiers) >= 2:
        precision, scale = t.modifiers[0], t.modifiers[1]
        max_abs = Decimal(10) ** (precision - scale) - Decimal(10) ** (-scale)
        return TypeSpec(kind="decimal", min_value=-max_abs, max_value=max_abs, places=scale)
    places = t.modifiers[1] if len(t.modifiers) > 1 else 2
    return TypeSpec(kind="decimal", min_value=-1_000_000, max_value=1_000_000, places=places)


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
    # tz-aware values for psycopg's adapter to type them correctly.
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
    "bytea": lambda _t: TypeSpec(kind="binary"),
    "vector": _vector,
}

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
