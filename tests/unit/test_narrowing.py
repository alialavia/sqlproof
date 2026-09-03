from __future__ import annotations

from sqlproof.generators.narrowing import narrow_spec_for_checks
from sqlproof.generators.typespec import spec_for_type
from sqlproof.schema.model import CheckConstraint, Column, PgType


def _col(name: str, type_name: str, **kw) -> Column:
    return Column(name, PgType("scalar", type_name, **kw), False, None, False)


def test_ge_zero_narrows_integer_lower_bound():
    col = _col("qty", "integer")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type), col, (CheckConstraint(expression="qty >= 0"),)
    )
    assert spec.min_value == 0
    assert spec.max_value == 2_147_483_647


def test_gt_zero_narrows_to_one_for_integers():
    col = _col("qty", "integer")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type), col, (CheckConstraint(expression="qty > 0"),)
    )
    assert spec.min_value == 1


def test_le_narrows_upper_bound():
    col = _col("pct", "integer")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type), col, (CheckConstraint(expression="pct <= 100"),)
    )
    assert spec.max_value == 100


def test_two_checks_compose():
    col = _col("pct", "integer")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type),
        col,
        (
            CheckConstraint(expression="pct >= 0"),
            CheckConstraint(expression="pct <= 100"),
        ),
    )
    assert (spec.min_value, spec.max_value) == (0, 100)


def test_in_set_narrows_to_enum():
    col = _col("tier", "text")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type),
        col,
        (CheckConstraint(expression="tier IN ('free', 'pro')"),),
    )
    assert spec.kind == "enum"
    assert set(spec.enum_values) == {"free", "pro"}


def test_length_check_narrows_text_max_size():
    col = _col("code", "text")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type),
        col,
        (CheckConstraint(expression="length(code) <= 8"),),
    )
    assert spec.max_size == 8


def test_unrecognised_check_leaves_spec_untouched():
    col = _col("a", "integer")
    original = spec_for_type(col.type)
    spec = narrow_spec_for_checks(
        original, col, (CheckConstraint(expression="a % 7 = position(x in y)"),)
    )
    assert spec == original


def test_check_on_a_different_column_is_ignored():
    col = _col("a", "integer")
    original = spec_for_type(col.type)
    spec = narrow_spec_for_checks(
        original, col, (CheckConstraint(expression="b >= 0"),)
    )
    assert spec == original


def test_check_wrapped_in_check_keyword_is_unwrapped():
    """Live-introspected checks come back as `CHECK (expr)` from
    `pg_get_constraintdef` (confirmed against a real Postgres 15
    instance -- `pg_get_constraintdef(oid, true)` on `CHECK (qty >= 0)`
    round-trips as exactly `CHECK (qty >= 0)`, no extra parens). The
    wrapper must be stripped before the shape regexes see the
    expression, matching constraints.py's own
    `_normalize_check_expression`."""
    col = _col("qty", "integer")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type), col, (CheckConstraint(expression="CHECK (qty >= 5)"),)
    )
    assert spec.min_value == 5


def test_any_array_narrows_to_enum_using_real_postgres_introspected_text():
    """Exactly what `pg_get_constraintdef(oid, true)` returns (verified
    against a live Postgres 15) for `CHECK (status IN ('draft',
    'published'))` on a text column -- Postgres canonicalizes an
    IN-list into `= ANY (ARRAY[...])` with a per-value `::text` cast,
    it never round-trips as `IN (...)`."""
    col = _col("status", "text")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type),
        col,
        (
            CheckConstraint(
                expression="CHECK (status = ANY (ARRAY['draft'::text, 'published'::text]))"
            ),
        ),
    )
    assert spec.kind == "enum"
    assert set(spec.enum_values) == {"draft", "published"}


def test_lt_narrows_upper_bound_exclusive_for_integers():
    col = _col("score", "integer")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type), col, (CheckConstraint(expression="score < 100"),)
    )
    assert spec.max_value == 99


def test_narrowing_never_widens_an_existing_bound():
    col = _col("pct", "integer")
    narrowed_once = narrow_spec_for_checks(
        spec_for_type(col.type), col, (CheckConstraint(expression="pct <= 50"),)
    )
    narrowed_again = narrow_spec_for_checks(
        narrowed_once, col, (CheckConstraint(expression="pct <= 200"),)
    )
    assert narrowed_again.max_value == 50


def test_any_array_narrows_to_enum():
    col = _col("priority", "integer")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type),
        col,
        (CheckConstraint(expression="priority = ANY(ARRAY[1, 2, 3, 5, 8])"),),
    )
    assert spec.kind == "enum"
    assert set(spec.enum_values) == {"1", "2", "3", "5", "8"}


def test_in_set_strips_type_cast_and_unescapes_quotes():
    col = _col("tier", "text")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type),
        col,
        (CheckConstraint(expression="tier IN ('free'::text, 'it''s pro')"),),
    )
    assert set(spec.enum_values) == {"free", "it's pro"}


def test_length_ge_and_le_compose_to_a_range():
    col = _col("code", "text")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type),
        col,
        (
            CheckConstraint(expression="length(code) >= 2"),
            CheckConstraint(expression="length(code) <= 8"),
        ),
    )
    assert (spec.min_size, spec.max_size) == (2, 8)


def test_length_eq_pins_exact_size():
    col = _col("code", "char", modifiers=(5,))
    spec = narrow_spec_for_checks(
        spec_for_type(col.type),
        col,
        (CheckConstraint(expression="length(code) = 5"),),
    )
    assert (spec.min_size, spec.max_size) == (5, 5)


def test_length_check_on_non_text_column_is_ignored():
    col = _col("count", "integer")
    original = spec_for_type(col.type)
    spec = narrow_spec_for_checks(
        original, col, (CheckConstraint(expression="length(count) = 3"),)
    )
    assert spec == original


def test_decimal_ge_with_fractional_literal_rounds_up_conservatively():
    """min_value/max_value are whole-number ends of a decimal range
    (`places` only governs drawn precision), so a fractional lower
    bound has to round toward the direction that can never admit a
    value the CHECK forbids -- up, not truncate-toward-zero."""
    col = _col("rate", "numeric", modifiers=(10, 4))
    spec = narrow_spec_for_checks(
        spec_for_type(col.type), col, (CheckConstraint(expression="rate >= 0.5"),)
    )
    assert spec.min_value == 1


def test_decimal_le_with_fractional_literal_rounds_down_conservatively():
    col = _col("rate", "numeric", modifiers=(10, 4))
    spec = narrow_spec_for_checks(
        spec_for_type(col.type), col, (CheckConstraint(expression="rate <= 0.5"),)
    )
    assert spec.max_value == 0


def test_decimal_le_with_negative_fractional_literal_uses_floor_not_truncation():
    """Plain `int(...)` truncates toward zero -- `int(Decimal("-0.5"))`
    is 0, not -1 -- which for an upper bound would round *up* and
    silently admit values (e.g. 0) that violate `rate <= -0.5`. Floor
    rounding (-0.5 -> -1) is the direction that can't do that."""
    col = _col("rate", "numeric", modifiers=(10, 4))
    spec = narrow_spec_for_checks(
        spec_for_type(col.type), col, (CheckConstraint(expression="rate <= -0.5"),)
    )
    assert spec.max_value == -1


def test_decimal_strict_gt_does_not_step_the_bound_by_one():
    """Unlike the integer case, a decimal spec's strict `>` keeps the
    rounded bound as-is -- there's no meaningful "next" decimal to
    step to at this TypeSpec's whole-number granularity."""
    col = _col("rate", "numeric", modifiers=(10, 4))
    spec = narrow_spec_for_checks(
        spec_for_type(col.type), col, (CheckConstraint(expression="rate > 1"),)
    )
    assert spec.min_value == 1
