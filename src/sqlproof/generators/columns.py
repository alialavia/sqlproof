from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy
from psycopg.types.range import Range

from sqlproof.exceptions import SqlProofSchemaError
from sqlproof.generators.typespec import TypeSpec, spec_for_type
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
    return strategy_for_spec(spec_for_type(pg_type))


def strategy_for_spec(spec: TypeSpec) -> SearchStrategy[Any]:
    if spec.kind == "integer":
        assert spec.min_value is not None and spec.max_value is not None
        # An integer spec's bounds are always plain `int` -- only a
        # decimal spec's can be `Decimal` (see typespec.py) -- so this
        # cast just narrows the field's static type.
        return st.integers(int(spec.min_value), int(spec.max_value))
    if spec.kind == "decimal":
        return st.decimals(
            min_value=Decimal(spec.min_value or 0),
            max_value=Decimal(spec.max_value or 0),
            places=spec.places,
            allow_nan=False,
            allow_infinity=False,
        )
    if spec.kind == "float":
        if spec.float_width == 32:
            return st.floats(width=32, allow_nan=False, allow_infinity=False)
        return st.floats(allow_nan=False, allow_infinity=False)
    if spec.kind == "boolean":
        return st.booleans()
    if spec.kind == "text":
        return _postgres_text(min_size=spec.min_size or 0, max_size=spec.max_size)
    if spec.kind == "uuid":
        return st.uuids().map(str)
    if spec.kind == "datetime":
        if spec.tz_aware:
            from datetime import UTC

            return st.datetimes(timezones=st.just(UTC))
        return st.datetimes()
    if spec.kind == "date":
        return st.dates()
    if spec.kind == "time":
        return st.times()
    if spec.kind == "interval":
        return st.timedeltas()
    if spec.kind == "json":
        # TypeSpec has no representation for the recursive JSON shape
        # (unlike range/composite, which nest via `element`/`fields`);
        # this is a known, accepted leak in the registry design, so
        # the recursion is hard-coded here rather than read from spec.
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
    if spec.kind == "binary":
        return st.binary()
    if spec.kind == "vector":
        assert spec.dimension is not None
        # Bounded integers rather than st.floats: bounded-float draws
        # spend ~9 bytes of Hypothesis entropy each, exhausting the 8KB
        # conjecture buffer at common embedding dimensions (1536, 2000).
        # Integers spend ~3. Shrink target is identical.
        component = st.integers(min_value=-1_000_000, max_value=1_000_000)
        return st.lists(component, min_size=spec.dimension, max_size=spec.dimension).map(
            lambda xs: "[" + ",".join(f"{x / 1_000_000:.6f}" for x in xs) + "]"
        )
    if spec.kind == "enum":
        return st.sampled_from(spec.enum_values)
    if spec.kind == "range":
        # Draws two element values, filters out the equal-pair case
        # (which would produce an empty Range with `[)` bounds —
        # technically valid in Postgres but almost never what a test
        # wants), then orders the pair so lower < upper. The `'[)'`
        # bounds match Postgres's canonical form for discrete range
        # types like int4range and daterange.
        #
        # tz-aware element specs (tstzrange, via spec_for_type) are
        # already resolved by the registry; only the range case needs
        # the timezone-aware variant for the wire format to match.
        #
        # Equal-pair collisions are extraordinarily rare for date and
        # datetime element strategies (Hypothesis's ``st.datetimes`` /
        # ``st.dates`` cover wide spans), and for numeric types the
        # integers / decimals strategies have plenty of headroom. The
        # filter is cheap.
        assert spec.element is not None
        element_strategy = strategy_for_spec(spec.element)
        return (
            st.tuples(element_strategy, element_strategy)
            .filter(lambda pair: pair[0] != pair[1])
            .map(lambda pair: Range(min(pair), max(pair), "[)"))
        )
    if spec.kind == "composite":
        # Recursive: a composite field's type can itself be another
        # composite; strategy_for_spec calls back into itself for
        # each field. Returned value is a dict so users can address
        # fields by name in property tests; matching the wire
        # format for INSERT is a follow-up that needs psycopg
        # composite-class registration.
        return st.fixed_dictionaries(
            {name: strategy_for_spec(sub) for name, sub in spec.fields}
        )
    raise SqlProofSchemaError(
        f"strategy_for_spec: unhandled SpecKind {spec.kind!r}; "
        "ensure typespec.py's KNOWN_TYPE_NAMES has a handler in this interpreter"
    )


def _postgres_text(*, min_size: int = 0, max_size: int | None = None) -> SearchStrategy[str]:
    return st.text(alphabet=POSTGRES_TEXT_ALPHABET, min_size=min_size, max_size=max_size)
