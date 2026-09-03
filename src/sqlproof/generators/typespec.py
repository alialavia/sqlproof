"""Declarative type knowledge, consumed by both generation paths.

This module holds what is *true* about a Postgres type — bounds,
sizes, precision, element types. It contains no Hypothesis strategies
and no RNG. `columns.py` interprets a TypeSpec into a Hypothesis
strategy; `bulk.py` interprets the same TypeSpec into a seeded
sampler.

Adding support for a new type means adding one entry here. Because
neither generator holds type knowledge of its own, it is not possible
to teach one path about a type and forget the other.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

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
    min_value: int | None = None
    max_value: int | None = None
    places: int | None = None
    min_size: int | None = None
    max_size: int | None = None
    float_width: int | None = None
    enum_values: tuple[str, ...] = ()
    dimension: int | None = None
    element: TypeSpec | None = None
    fields: tuple[tuple[str, TypeSpec], ...] = ()
    tz_aware: bool = False


def _int(lo: int, hi: int) -> Callable[[PgType], TypeSpec]:
    return lambda _t: TypeSpec(kind="integer", min_value=lo, max_value=hi)


def _numeric(t: PgType) -> TypeSpec:
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


def _timestamp(t: PgType) -> TypeSpec:
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
