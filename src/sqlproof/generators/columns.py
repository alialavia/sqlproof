from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

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
    if name in {"smallint", "int2"}:
        return st.integers(-32_768, 32_767)
    if name in {"integer", "int", "int4", "serial"}:
        return st.integers(-2_147_483_648, 2_147_483_647)
    if name in {"bigint", "int8", "bigserial"}:
        return st.integers(-(2**63), 2**63 - 1)
    if name in {"numeric", "decimal"}:
        places = pg_type.modifiers[1] if len(pg_type.modifiers) > 1 else 2
        return st.decimals(
            min_value=Decimal("-1000000"),
            max_value=Decimal("1000000"),
            places=places,
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
    if name in {"char", "character"}:
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
    return _postgres_text(max_size=255)


def _postgres_text(*, min_size: int = 0, max_size: int | None = None) -> SearchStrategy[str]:
    return st.text(alphabet=POSTGRES_TEXT_ALPHABET, min_size=min_size, max_size=max_size)
