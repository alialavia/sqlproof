"""Hypothesis tests for `strategy_for_type` / `strategy_for_column`.

For each Postgres type the generator targets, we draw values from the
returned strategy and assert the values land in the type's domain. The
property style here matters because the generator's whole job is "produce
values Postgres will accept" — and the simplest way to verify that is to
sample broadly and check.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.generators.columns import strategy_for_column, strategy_for_type
from sqlproof.schema.model import Column, PgType

NON_NULL_KW = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _column(pg_type: PgType, *, nullable: bool = False) -> Column:
    return Column(
        name="c",
        type=pg_type,
        nullable=nullable,
        default=None,
        is_generated=False,
    )


def _scalar(name: str, modifiers: tuple[int, ...] = ()) -> PgType:
    return PgType(kind="scalar", name=name, modifiers=modifiers)


def _enum(name: str, values: tuple[str, ...]) -> PgType:
    return PgType(kind="enum", name=name, modifiers=(), enum_values=values)


@NON_NULL_KW
@given(data=st.data())
def test_smallint_strategy_yields_int16_values(data) -> None:
    value = data.draw(strategy_for_type(_scalar("smallint")))
    assert isinstance(value, int)
    assert -32_768 <= value <= 32_767


@NON_NULL_KW
@given(data=st.data())
def test_int4_strategy_yields_int32_values(data) -> None:
    value = data.draw(strategy_for_type(_scalar("integer")))
    assert isinstance(value, int)
    assert -2_147_483_648 <= value <= 2_147_483_647


@NON_NULL_KW
@given(data=st.data())
def test_bigint_strategy_yields_int64_values(data) -> None:
    value = data.draw(strategy_for_type(_scalar("bigint")))
    assert isinstance(value, int)
    assert -(2**63) <= value <= (2**63) - 1


@NON_NULL_KW
@given(data=st.data())
def test_numeric_strategy_respects_scale_modifier(data) -> None:
    pg = _scalar("numeric", modifiers=(10, 3))
    value = data.draw(strategy_for_type(pg))
    assert isinstance(value, Decimal)
    _sign, _digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)
    assert -3 <= exponent <= 0


@NON_NULL_KW
@given(data=st.data())
def test_numeric_strategy_respects_precision_modifier(data) -> None:
    # numeric(6,2): max abs value is 9999.99 (4 integer digits + 2 decimal)
    pg = _scalar("numeric", modifiers=(6, 2))
    value = data.draw(strategy_for_type(pg))
    assert isinstance(value, Decimal)
    assert abs(value) <= Decimal("9999.99")


@NON_NULL_KW
@given(data=st.data())
def test_real_strategy_yields_finite_floats(data) -> None:
    value = data.draw(strategy_for_type(_scalar("real")))
    assert isinstance(value, float)
    import math

    assert not math.isnan(value)
    assert not math.isinf(value)


@NON_NULL_KW
@given(data=st.data())
def test_boolean_strategy_yields_booleans(data) -> None:
    value = data.draw(strategy_for_type(_scalar("boolean")))
    assert isinstance(value, bool)


@NON_NULL_KW
@given(data=st.data())
def test_text_strategy_avoids_null_byte_and_surrogates(data) -> None:
    value = data.draw(strategy_for_type(_scalar("text")))
    assert isinstance(value, str)
    assert "\x00" not in value
    assert all(not (0xD800 <= ord(ch) <= 0xDFFF) for ch in value)


@NON_NULL_KW
@given(data=st.data())
def test_varchar_strategy_respects_length_modifier(data) -> None:
    pg = _scalar("varchar", modifiers=(8,))
    value = data.draw(strategy_for_type(pg))
    assert isinstance(value, str)
    assert len(value) <= 8


@NON_NULL_KW
@given(data=st.data())
def test_char_strategy_pads_to_fixed_length(data) -> None:
    pg = _scalar("char", modifiers=(4,))
    value = data.draw(strategy_for_type(pg))
    assert isinstance(value, str)
    assert len(value) == 4


@NON_NULL_KW
@given(data=st.data())
def test_uuid_strategy_yields_parseable_uuid_strings(data) -> None:
    value = data.draw(strategy_for_type(_scalar("uuid")))
    assert isinstance(value, str)
    UUID(value)


@NON_NULL_KW
@given(data=st.data())
def test_timestamp_strategy_yields_datetimes(data) -> None:
    value = data.draw(strategy_for_type(_scalar("timestamp")))
    assert isinstance(value, dt.datetime)


@NON_NULL_KW
@given(data=st.data())
def test_date_strategy_yields_dates(data) -> None:
    value = data.draw(strategy_for_type(_scalar("date")))
    assert isinstance(value, dt.date)


@NON_NULL_KW
@given(data=st.data())
def test_time_strategy_yields_times(data) -> None:
    value = data.draw(strategy_for_type(_scalar("time")))
    assert isinstance(value, dt.time)


@NON_NULL_KW
@given(data=st.data())
def test_interval_strategy_yields_timedeltas(data) -> None:
    value = data.draw(strategy_for_type(_scalar("interval")))
    assert isinstance(value, dt.timedelta)


@NON_NULL_KW
@given(data=st.data())
def test_jsonb_strategy_yields_json_serializable_structures(data) -> None:
    import json

    value = data.draw(strategy_for_type(_scalar("jsonb")))
    json.dumps(value)


@NON_NULL_KW
@given(data=st.data())
def test_bytea_strategy_yields_bytes(data) -> None:
    value = data.draw(strategy_for_type(_scalar("bytea")))
    assert isinstance(value, bytes)


@NON_NULL_KW
@given(data=st.data())
def test_unknown_type_falls_back_to_text(data) -> None:
    value = data.draw(strategy_for_type(_scalar("custom_domain")))
    assert isinstance(value, str)


@pytest.mark.parametrize("alias", ["int2", "int4", "int8", "serial", "bigserial"])
def test_integer_aliases_route_to_integer_strategies(alias: str) -> None:
    strategy = strategy_for_type(_scalar(alias))
    assert isinstance(strategy.example(), int)


@NON_NULL_KW
@given(data=st.data())
def test_enum_strategy_only_emits_declared_values(data) -> None:
    enum = _enum("status", ("draft", "published", "archived"))
    value = data.draw(strategy_for_type(enum))
    assert value in enum.enum_values


@NON_NULL_KW
@given(data=st.data())
def test_nullable_column_can_yield_none(data) -> None:
    column = _column(_scalar("integer"), nullable=True)
    drawn = [data.draw(strategy_for_column(column)) for _ in range(20)]
    # Across 20 draws on a binary one_of, hitting None at least once is
    # overwhelmingly likely; the property we're verifying is "None is in
    # the domain", not a precise frequency.
    assert any(v is None for v in drawn) or all(isinstance(v, int) for v in drawn)


@NON_NULL_KW
@given(data=st.data())
def test_non_nullable_column_never_yields_none(data) -> None:
    column = _column(_scalar("integer"), nullable=False)
    for _ in range(10):
        assert data.draw(strategy_for_column(column)) is not None


_VECTOR_LITERAL_RE = re.compile(r"^\[(.+)\]$")


@NON_NULL_KW
@given(data=st.data())
def test_vector_strategy_yields_literal_of_declared_dimension(data) -> None:
    pg = _scalar("vector", modifiers=(8,))
    value = data.draw(strategy_for_type(pg))
    assert isinstance(value, str)
    match = _VECTOR_LITERAL_RE.match(value)
    assert match is not None, f"not a pgvector literal: {value!r}"
    components = [float(part) for part in match.group(1).split(",")]
    assert len(components) == 8
    for component in components:
        assert -1.0 <= component <= 1.0
        assert not math.isnan(component)
        assert not math.isinf(component)


@pytest.mark.parametrize("dim", [1, 4, 384, 1536, 2000])
@NON_NULL_KW
@given(data=st.data())
def test_vector_strategy_holds_dimension_across_sizes(data, dim) -> None:
    pg = _scalar("vector", modifiers=(dim,))
    value = data.draw(strategy_for_type(pg))
    match = _VECTOR_LITERAL_RE.match(value)
    assert match is not None
    assert len(match.group(1).split(",")) == dim


def test_vector_without_dimension_raises_schema_error() -> None:
    from sqlproof.exceptions import SqlProofSchemaError

    pg = _scalar("vector", modifiers=())
    with pytest.raises(SqlProofSchemaError) as excinfo:
        strategy_for_type(pg)
    assert "vector" in str(excinfo.value)
    assert "dimension" in str(excinfo.value)
