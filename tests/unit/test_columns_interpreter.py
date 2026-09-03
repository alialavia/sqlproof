from __future__ import annotations

from decimal import Decimal

from hypothesis import given, settings

from sqlproof.generators.columns import strategy_for_type
from sqlproof.schema.model import PgType


@given(strategy_for_type(PgType("scalar", "smallint")))
@settings(max_examples=25, deadline=None)
def test_smallint_stays_in_range(v):
    assert -32_768 <= v <= 32_767


@given(strategy_for_type(PgType("scalar", "varchar", modifiers=(12,))))
@settings(max_examples=25, deadline=None)
def test_varchar_respects_modifier(v):
    assert len(v) <= 12
    assert "\x00" not in v


@given(strategy_for_type(PgType("scalar", "numeric", modifiers=(10, 3))))
@settings(max_examples=25, deadline=None)
def test_numeric_respects_scale(v):
    assert isinstance(v, Decimal)
    assert -Decimal("1000000") <= v <= Decimal("1000000")
