"""Hypothesis tests for `refine_for_checks` and the helpers it composes.

The CHECK refinement pipeline takes a column's base strategy and a tuple of
CHECK constraints, and returns a stricter strategy whose drawn values
satisfy every constraint. We verify this by:

  * generating values from the refined strategy and asserting the
    constraint predicate evaluates true on each one,
  * exercising every shape of CHECK the refiner recognizes (IN-set,
    ANY(ARRAY[...]), length comparison, range comparison),
  * checking that unrecognized expressions fall back to a permissive
    strategy without raising.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof.generators.columns import strategy_for_column
from sqlproof.generators.constraints import (
    refine_for_checks,
    unique_rows,
)
from sqlproof.schema.model import CheckConstraint, Column, PgType

NON_NULL_KW = settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


def _column(
    name: str,
    type_name: str,
    *,
    modifiers: tuple[int, ...] = (),
    nullable: bool = False,
) -> Column:
    return Column(
        name=name,
        type=PgType(kind="scalar", name=type_name, modifiers=modifiers),
        nullable=nullable,
        default=None,
        is_generated=False,
    )


def _check(expression: str) -> CheckConstraint:
    return CheckConstraint(expression=expression)


@NON_NULL_KW
@given(data=st.data())
def test_in_set_check_restricts_strategy_to_listed_values(data) -> None:
    column = _column("status", "text")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(
        column, strategy, (_check("status IN ('draft', 'published', 'archived')"),)
    )
    value = data.draw(refined)
    assert value in {"draft", "published", "archived"}


@NON_NULL_KW
@given(data=st.data())
def test_any_array_check_restricts_strategy_to_listed_values(data) -> None:
    column = _column("priority", "integer")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(
        column, strategy, (_check("priority = ANY(ARRAY[1, 2, 3, 5, 8])"),)
    )
    value = data.draw(refined)
    assert value in {1, 2, 3, 5, 8}


@NON_NULL_KW
@given(data=st.data())
def test_range_ge_check_yields_values_at_or_above_threshold(data) -> None:
    column = _column("price", "integer")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("price >= 100"),))
    value = data.draw(refined)
    assert value >= 100


@NON_NULL_KW
@given(data=st.data())
def test_range_gt_check_yields_values_strictly_above_threshold(data) -> None:
    column = _column("count", "integer")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("count > 0"),))
    value = data.draw(refined)
    assert value > 0


@NON_NULL_KW
@given(data=st.data())
def test_range_le_check_filters_to_values_at_or_below_threshold(data) -> None:
    column = _column("temperature", "integer")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("temperature <= 50"),))
    value = data.draw(refined)
    assert value <= 50


@NON_NULL_KW
@given(data=st.data())
def test_range_lt_check_filters_to_values_strictly_below_threshold(data) -> None:
    column = _column("score", "integer")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("score < 100"),))
    value = data.draw(refined)
    assert value < 100


@NON_NULL_KW
@given(data=st.data())
def test_numeric_range_ge_yields_decimals_at_or_above_threshold(data) -> None:
    column = _column("rate", "numeric", modifiers=(10, 4))
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("rate >= 0.5"),))
    value = data.draw(refined)
    assert value >= Decimal("0.5")


@NON_NULL_KW
@given(data=st.data())
def test_length_eq_check_pins_string_length(data) -> None:
    column = _column("country_code", "char", modifiers=(2,))
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("length(country_code) = 2"),))
    value = data.draw(refined)
    assert isinstance(value, str)
    assert len(value) == 2


@NON_NULL_KW
@given(data=st.data())
def test_length_le_check_caps_string_length(data) -> None:
    column = _column("nick", "varchar", modifiers=(50,))
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("length(nick) <= 8"),))
    value = data.draw(refined)
    assert isinstance(value, str)
    assert len(value) <= 8


@NON_NULL_KW
@given(data=st.data())
def test_length_lt_check_excludes_threshold(data) -> None:
    column = _column("short", "varchar", modifiers=(50,))
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("length(short) < 5"),))
    value = data.draw(refined)
    assert isinstance(value, str)
    assert len(value) < 5


@NON_NULL_KW
@given(data=st.data())
def test_length_ge_check_enforces_lower_bound(data) -> None:
    column = _column("token", "text")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("length(token) >= 4"),))
    value = data.draw(refined)
    assert isinstance(value, str)
    assert len(value) >= 4


@NON_NULL_KW
@given(data=st.data())
def test_length_gt_check_strictly_above_threshold(data) -> None:
    column = _column("phrase", "text")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("length(phrase) > 3"),))
    value = data.draw(refined)
    assert isinstance(value, str)
    assert len(value) > 3


def test_unrecognized_check_falls_through_to_unrefined_strategy() -> None:
    column = _column("name", "text")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(
        column, strategy, (_check("name LIKE '%@%' OR name SIMILAR TO '%foo%'"),)
    )
    # Drawing should still work — refiner just returns the original strategy.
    refined.example()


def test_check_expression_wrapped_in_check_keyword_is_unwrapped() -> None:
    column = _column("status", "text")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(
        column, strategy, (_check("CHECK (status IN ('a', 'b'))"),)
    )
    assert refined.example() in {"a", "b"}


def test_in_set_with_quoted_value_handles_doubled_apostrophes() -> None:
    column = _column("note", "text")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("note IN ('it''s ok', 'fine')"),))
    assert refined.example() in {"it's ok", "fine"}


def test_in_set_with_typed_cast_strips_cast_suffix() -> None:
    column = _column("tier", "text")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(
        column, strategy, (_check("tier IN ('free'::text, 'pro'::text)"),)
    )
    assert refined.example() in {"free", "pro"}


def test_unique_rows_returns_true_when_keys_are_distinct() -> None:
    rows = [{"id": 1, "tag": "a"}, {"id": 2, "tag": "b"}, {"id": 3, "tag": "a"}]
    assert unique_rows(rows, ("id",)) is True


def test_unique_rows_returns_false_when_composite_key_repeats() -> None:
    rows = [{"id": 1, "tag": "a"}, {"id": 1, "tag": "a"}, {"id": 2, "tag": "b"}]
    assert unique_rows(rows, ("id", "tag")) is False


def test_unique_rows_treats_composite_keys_independently() -> None:
    rows = [{"id": 1, "tag": "a"}, {"id": 1, "tag": "b"}]
    assert unique_rows(rows, ("id", "tag")) is True


@NON_NULL_KW
@given(data=st.data())
def test_range_check_on_smallint_falls_back_to_filter_predicate(data) -> None:
    """smallint isn't in `_direct_range_strategy`'s fast-path; the refiner
    must wrap with `.filter(...)` and still produce values that satisfy
    the check."""
    column = _column("priority", "smallint")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("priority >= 10"),))
    value = data.draw(refined)
    assert value >= 10


@NON_NULL_KW
@given(data=st.data())
def test_range_check_on_smallint_with_gt_op_uses_predicate(data) -> None:
    column = _column("level", "smallint")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("level > 5"),))
    value = data.draw(refined)
    assert value > 5


@NON_NULL_KW
@given(data=st.data())
def test_predicate_filter_admits_null_for_nullable_column(data) -> None:
    """The fall-through predicate explicitly admits None so nullable
    columns aren't starved by the filter."""
    column = _column("opt_score", "smallint", nullable=True)
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("opt_score >= 1"),))
    value = data.draw(refined)
    assert value is None or value >= 1


@NON_NULL_KW
@given(data=st.data())
def test_in_set_with_bare_identifier_token_is_kept_as_string(data) -> None:
    """When an IN-list value isn't a quoted string or a number,
    `_parse_sql_literal` returns it verbatim — the refiner still produces
    something the column can hold."""
    column = _column("flag", "text")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("flag IN (foo, bar)"),))
    value = data.draw(refined)
    assert value in {"foo", "bar"}


@NON_NULL_KW
@given(data=st.data())
def test_length_check_on_non_text_column_falls_through_unrefined(data) -> None:
    """`_direct_length_strategy` returns None for non-text types, and the
    refiner falls through to the original strategy without raising."""
    column = _column("count", "integer")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("length(count) = 3"),))
    value = data.draw(refined)
    assert isinstance(value, int)


def test_length_check_yielding_impossible_bounds_returns_st_nothing() -> None:
    """If a length check pushes min above the column-type cap, the helper
    should return `st.nothing()` — drawing from it raises `Unsatisfiable`."""
    from hypothesis.errors import Unsatisfiable

    column = _column("token", "varchar", modifiers=(5,))
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("length(token) >= 100"),))
    with pytest.raises(Unsatisfiable):
        refined.example()


@NON_NULL_KW
@given(data=st.data())
def test_bigint_range_uses_direct_strategy_within_int64_bounds(data) -> None:
    column = _column("offset", "bigint")
    strategy = strategy_for_column(column)
    refined = refine_for_checks(column, strategy, (_check("offset >= 0"),))
    value = data.draw(refined)
    assert 0 <= value <= 2**63 - 1


