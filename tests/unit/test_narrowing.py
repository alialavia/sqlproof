from __future__ import annotations

import random
from decimal import Decimal

from sqlproof.generators.bulk import sampler_for_spec
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
    assert set(spec.enum_values) == {1, 2, 3, 5, 8}
    assert all(isinstance(v, int) for v in spec.enum_values)


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


def test_decimal_strict_gt_steps_the_bound_like_integers_do():
    """`min_value`/`max_value` are typed `int | None`, so an exclusive
    decimal bound has no exact representation -- there's no int
    "just above 1". Not stepping (the brief's original behaviour)
    leaves `min_value == 1`, which is exactly the forbidden endpoint:
    a decimal sampler quantising a continuous draw to `places` can and
    does land exactly on a whole-number bound often enough to matter
    (see `test_decimal_strict_bound_draws_never_hit_the_forbidden_endpoint`,
    which caught this against the real sampler). Stepping by a whole 1,
    the same as the integer case, over-narrows (losing 1.0001-1.9999)
    but can never admit 1 itself."""
    col = _col("rate", "numeric", modifiers=(10, 4))
    spec = narrow_spec_for_checks(
        spec_for_type(col.type), col, (CheckConstraint(expression="rate > 1"),)
    )
    assert spec.min_value == 2


def test_decimal_strict_lt_steps_the_bound_like_integers_do():
    col = _col("rate", "numeric", modifiers=(10, 4))
    spec = narrow_spec_for_checks(
        spec_for_type(col.type), col, (CheckConstraint(expression="rate < 5"),)
    )
    assert spec.max_value == 4


def test_decimal_strict_bound_draws_never_hit_the_forbidden_endpoint():
    """Regression for the actual bug: run the real `bulk.py` decimal
    sampler (not just inspect the narrowed spec) against a `rate > 1`
    CHECK and confirm none of many draws round-trips to exactly
    `Decimal("1.0000")`. Before the fix, `min_value` stayed at 1 and
    this failed roughly 1 in 10,000 draws (quantising a continuous
    draw near the boundary snaps it onto the boundary)."""
    col = _col("rate", "numeric", modifiers=(10, 4))
    spec = narrow_spec_for_checks(
        spec_for_type(col.type), col, (CheckConstraint(expression="rate > 1"),)
    )
    rng = random.Random(20260903)
    sampler = sampler_for_spec(spec, rng)
    draws = [sampler() for _ in range(20_000)]
    assert Decimal("1.0000") not in draws


def test_integer_in_set_yields_ints_not_strings():
    """`qty IN (1, 2, 3)` on an INTEGER column must produce `int`
    values, not the strings a naive literal parse would give -- both
    interpreters hand `enum_values` straight to a sampler with no
    coercion, and a Python `str` fed to an integer column fails at
    insert time."""
    col = _col("priority", "integer")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type),
        col,
        (CheckConstraint(expression="priority IN (1, 2, 3)"),),
    )
    assert spec.enum_values == (1, 2, 3)
    assert all(isinstance(v, int) and not isinstance(v, bool) for v in spec.enum_values)


def test_decimal_in_set_yields_decimals_not_strings():
    col = _col("rate", "numeric", modifiers=(10, 4))
    spec = narrow_spec_for_checks(
        spec_for_type(col.type),
        col,
        (CheckConstraint(expression="rate IN (1.5, 2.5)"),),
    )
    assert set(spec.enum_values) == {Decimal("1.5"), Decimal("2.5")}
    assert all(isinstance(v, Decimal) for v in spec.enum_values)


def test_text_in_set_still_yields_strings():
    """The enum-value coercion must not disturb the already-passing
    text case: a genuine Postgres enum's labels, and a text column's
    IN-list, both legitimately stay `str`."""
    col = _col("tier", "text")
    spec = narrow_spec_for_checks(
        spec_for_type(col.type),
        col,
        (CheckConstraint(expression="tier IN ('free', 'pro')"),),
    )
    assert all(isinstance(v, str) for v in spec.enum_values)
