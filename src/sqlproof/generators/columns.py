from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy
from psycopg.types.range import Range

from sqlproof.exceptions import SqlProofSchemaError
from sqlproof.schema.model import Column, PgType

_POSTGRES_BLACKLIST_CATEGORIES: tuple[Literal["Cs"], ...] = ("Cs",)

POSTGRES_TEXT_ALPHABET = st.characters(
    blacklist_characters="\x00",
    blacklist_categories=_POSTGRES_BLACKLIST_CATEGORIES,
)


def strategy_for_column(column: Column) -> SearchStrategy[Any]:
    strategy = strategy_for_type(column.type)
    if column.nullable:
        strategy = st.one_of(st.none(), strategy)
    return strategy


def strategy_for_type(pg_type: PgType) -> SearchStrategy[Any]:
    name = pg_type.name.lower()
    if pg_type.kind == "enum":
        return st.sampled_from(pg_type.enum_values)
    if pg_type.kind == "domain" and pg_type.base is not None:
        # Domain types are alias + optional CHECKs. Strategy comes
        # from the base type; CHECK enforcement happens at column-
        # generation time in `rows.py` via the existing refinement
        # pipeline (which knows the actual column name to substitute
        # for the `VALUE` placeholder in the CHECK expressions).
        return strategy_for_type(pg_type.base)
    if pg_type.kind == "range" and pg_type.base is not None:
        return _range_strategy(pg_type)
    if pg_type.kind == "composite":
        # Recursive: a composite field's type can itself be another
        # composite; strategy_for_type calls back into itself for
        # each field. Returned value is a dict so users can address
        # fields by name in property tests; matching the wire
        # format for INSERT is a follow-up that needs psycopg
        # composite-class registration.
        return st.fixed_dictionaries(
            {fname: strategy_for_type(ftype) for fname, ftype in pg_type.composite_fields}
        )
    if name in {"smallint", "int2"}:
        return st.integers(-32_768, 32_767)
    if name in {"integer", "int", "int4", "serial"}:
        return st.integers(-2_147_483_648, 2_147_483_647)
    if name in {"bigint", "int8", "bigserial"}:
        return st.integers(-(2**63), 2**63 - 1)
    if name in {"numeric", "decimal"}:
        if len(pg_type.modifiers) >= 2:
            precision, scale = pg_type.modifiers[0], pg_type.modifiers[1]
            max_abs = Decimal(10) ** (precision - scale) - Decimal(10) ** (-scale)
        else:
            scale = 2
            max_abs = Decimal("1000000")
        return st.decimals(
            min_value=-max_abs,
            max_value=max_abs,
            places=scale,
            allow_nan=False,
            allow_infinity=False,
        )
    if name in {"real", "float4"}:
        return st.floats(width=32, allow_nan=False, allow_infinity=False)
    if name in {"double precision", "float8"}:
        return st.floats(allow_nan=False, allow_infinity=False)
    if name in {"boolean", "bool"}:
        return st.booleans()
    if name in {"text", "citext"}:
        return _postgres_text(max_size=255)
    if name in {"varchar", "character varying"}:
        max_size = pg_type.modifiers[0] if pg_type.modifiers else 255
        return _postgres_text(max_size=max_size)
    if name in {"char", "character", "bpchar"}:
        size = pg_type.modifiers[0] if pg_type.modifiers else 1
        return _postgres_text(min_size=size, max_size=size)
    if name == "uuid":
        return st.uuids().map(str)
    if name in {
        "timestamp",
        "timestamp without time zone",
        "timestamptz",
        "timestamp with time zone",
    }:
        return st.datetimes()
    if name == "date":
        return st.dates()
    if name in {"time", "timetz"}:
        return st.times()
    if name == "interval":
        return st.timedeltas()
    if name in {"json", "jsonb"}:
        json_scalar = (
            st.none()
            | st.booleans()
            | st.floats(allow_nan=False, allow_infinity=False)
            | _postgres_text()
        )
        return st.recursive(
            json_scalar,
            lambda children: (
                st.lists(children, max_size=5)
                | st.dictionaries(_postgres_text(max_size=20), children, max_size=5)
            ),
            max_leaves=10,
        )
    if name == "bytea":
        return st.binary()
    if name == "vector":
        if not pg_type.modifiers:
            raise SqlProofSchemaError(
                "vector type requires a dimension (e.g. vector(384)); "
                "got vector with no modifier"
            )
        dim = pg_type.modifiers[0]
        # Use bounded integers + scale-to-float instead of st.floats:
        # Hypothesis's bounded-float draws spend enough entropy per
        # value (~9 bytes) that the default 8KB conjecture buffer is
        # exhausted at common embedding sizes (1536, 2000). Bounded
        # integers spend ~3 bytes per draw, leaving headroom for the
        # full range of real-world vector dimensions. Shrink target
        # is identical (integers shrink toward 0, scale-map preserves
        # that). 6-digit decimal resolution per component is well
        # within pgvector's float32 storage precision.
        component = st.integers(min_value=-1_000_000, max_value=1_000_000)
        return (
            st.lists(component, min_size=dim, max_size=dim)
            .map(
                lambda xs: "["
                + ",".join(f"{x / 1_000_000:.6f}" for x in xs)
                + "]"
            )
        )
    return _postgres_text(max_size=255)


def _postgres_text(*, min_size: int = 0, max_size: int | None = None) -> SearchStrategy[str]:
    return st.text(alphabet=POSTGRES_TEXT_ALPHABET, min_size=min_size, max_size=max_size)


def _range_strategy(pg_type: PgType) -> SearchStrategy[Range[Any]]:
    """Build a psycopg Range strategy from the element type.

    Draws two element values, filters out the equal-pair case
    (which would produce an empty Range with `[)` bounds —
    technically valid in Postgres but almost never what a test
    wants), then orders the pair so lower < upper. The ``'[)'``
    bounds match Postgres's canonical form for discrete range
    types like int4range and daterange.

    For ``tstzrange`` we override the element strategy to emit
    timezone-aware datetimes (Postgres rejects ``tstzrange``
    populated with naive datetimes — they're typed as
    ``tsrange`` by psycopg's adapter). The plain ``timestamptz``
    column path continues to use naive datetimes via
    ``strategy_for_type``; only the range case needs the
    timezone-aware variant for the wire format to match.

    Equal-pair collisions are extraordinarily rare for date and
    datetime element strategies (Hypothesis's ``st.datetimes`` /
    ``st.dates`` cover wide spans), and for numeric types the
    integers / decimals strategies have plenty of headroom. The
    filter is cheap.
    """
    assert pg_type.base is not None
    if pg_type.name == "tstzrange":
        from datetime import UTC

        element_strategy: SearchStrategy[Any] = st.datetimes(timezones=st.just(UTC))
    else:
        element_strategy = strategy_for_type(pg_type.base)
    return (
        st.tuples(element_strategy, element_strategy)
        .filter(lambda pair: pair[0] != pair[1])
        .map(lambda pair: Range(min(pair), max(pair), "[)"))
    )
