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
def test_numeric_respects_precision_and_scale(v):
    # numeric(10, 3) admits 10 - 3 = 7 integer digits, so the largest
    # legal magnitude is 9999999.999. This assertion previously pinned a
    # flat +/-1000000 regardless of precision, which was the pre-#101
    # behaviour the interpreter inherited -- it encoded the overflow bug
    # as expected behaviour.
    assert isinstance(v, Decimal)
    assert -Decimal("9999999.999") <= v <= Decimal("9999999.999")
    # And check the scale the test's name claims, which the original
    # never did.
    assert -v.as_tuple().exponent <= 3
