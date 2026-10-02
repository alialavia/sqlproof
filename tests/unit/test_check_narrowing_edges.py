"""How CHECK facts combine with each other and with the column's type.

`test_check_shapes.py` covers each shape on its own. These cover the
interactions: an IN-list meeting an earlier bound or exclusion, a
Postgres enum meeting an IN-list, fixed-width `char(n)` padding, float
columns (where Python's float/Decimal equality differs), contradictory
CHECKs, and literals that can't belong to the column's type (possible
in a schema file, which Postgres never validated).
"""

from __future__ import annotations

import random
import struct
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis.errors import Unsatisfiable

from sqlproof.exceptions import SqlProofGenerationError
from sqlproof.generators.bulk import sampler_for_spec
from sqlproof.generators.columns import strategy_for_column
from sqlproof.generators.constraints import refine_for_checks
from sqlproof.generators.narrowing import narrow_spec_for_checks
from sqlproof.generators.typespec import (
    TypeSpec,
    float32_ceil,
    float32_floor,
    spec_for_type,
    spec_is_empty,
)
from sqlproof.schema.model import CheckConstraint, Column, PgType

SETTINGS = settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow])


def _column(name: str, pg_type: PgType) -> Column:
    return Column(name=name, type=pg_type, nullable=False, default=None, is_generated=False)


def _scalar(name: str, type_name: str, *modifiers: int) -> Column:
    return _column(name, PgType("scalar", type_name, modifiers=modifiers))


def _narrow(column: Column, *checks: str) -> TypeSpec:
    return narrow_spec_for_checks(
        spec_for_type(column.type), column, tuple(CheckConstraint(c) for c in checks)
    )


@pytest.mark.parametrize(
    ("column", "checks", "allowed"),
    [
        # A length bound filters an earlier IN-list instead of being lost.
        (_scalar("status", "text"), ("status IN ('a', 'bbb')", "length(status) > 1"), ("bbb",)),
        (_scalar("status", "text"), ("status IN ('a', 'bbb')", "length(status) < 2"), ("a",)),
        (_scalar("status", "text"), ("status IN ('a', 'bbb')", "length(status) = 3"), ("bbb",)),
        # An exclusion before an IN-list removes that value from it.
        (_scalar("x", "integer"), ("x <> 2", "x IN (1, 2, 3)"), (1, 3)),
        # A NOT IN after an IN-list removes from it too.
        (_scalar("x", "integer"), ("x IN (1, 2, 3)", "x NOT IN (2)"), (1, 3)),
        # Earlier numeric bounds filter a later IN-list on both sides.
        (_scalar("x", "integer"), ("x >= 2", "x IN (1, 2, 3)"), (2, 3)),
        (_scalar("x", "integer"), ("x <= 2", "x IN (1, 2, 3)"), (1, 2)),
        # IN-list values longer than varchar(n) can't be stored.
        (_scalar("code", "varchar", 3), ("code IN ('ab', 'abcd')",), ("ab",)),
        # Booleans keep their type rather than becoming strings.
        (_scalar("flag", "boolean"), ("flag IN (true)",), (True,)),
        # Floats: 0.1 != Decimal("0.1") in Python, yet it is the value
        # the CHECK excludes.
        (_scalar("r", "double precision"), ("r IN (0.1, 0.2)", "r <> 0.1"), (0.2,)),
        # A text column compared with a bare number (schema-file only;
        # Postgres would reject it) is coerced to its text form.
        (_scalar("code", "text"), ("code IN (1, 2)",), ("1", "2")),
    ],
)
def test_combined_checks_narrow_to_their_intersection(
    column: Column, checks: tuple[str, ...], allowed: tuple[Any, ...]
) -> None:
    spec = _narrow(column, *checks)
    assert spec.kind == "enum"
    assert spec.enum_values == allowed
    refined = refine_for_checks(
        column, strategy_for_column(column), tuple(CheckConstraint(c) for c in checks)
    )

    @SETTINGS
    @given(value=refined)
    def run(value: Any) -> None:
        assert value in allowed

    run()


def test_in_list_on_a_postgres_enum_intersects_its_labels() -> None:
    mood = PgType("enum", "mood", enum_values=("happy", "sad", "meh"))
    column = _column("m", mood)
    assert _narrow(column, "m IN ('happy', 'sad', 'angry')").enum_values == ("happy", "sad")
    assert _narrow(column, "m <> 'meh'").enum_values == ("happy", "sad")


@pytest.mark.parametrize(
    "pg_type",
    [
        PgType("scalar", "char", modifiers=(2,)),
        # Through a domain the underlying char(2) still governs.
        PgType("domain", "code2", base=PgType("scalar", "bpchar", modifiers=(2,))),
    ],
    ids=["char", "domain_over_char"],
)
def test_short_in_list_values_are_kept_for_fixed_width_char(pg_type: PgType) -> None:
    # Postgres pads 'a' to 'a ' in a char(2) and compares ignoring
    # trailing blanks, so `c IN ('a')` is satisfiable.
    spec = _narrow(_column("c", pg_type), "c IN ('a', 'bb')")
    assert spec.enum_values == ("a", "bb")


@pytest.mark.parametrize(
    ("type_name", "literal"),
    [
        ("integer", "'abc'"),
        ("integer", "true"),
        ("numeric", "'abc'"),
        ("double precision", "'abc'"),
    ],
)
def test_in_list_literal_impossible_for_the_column_type_is_unsatisfiable(
    type_name: str, literal: str
) -> None:
    # Only reachable from an unvalidated schema file. Generation must
    # fail loudly rather than crash or emit a value Postgres rejects.
    column = _scalar("x", type_name)
    spec = _narrow(column, f"x IN ({literal})")
    assert spec_is_empty(spec)
    with pytest.raises(SqlProofGenerationError, match="admit no value"):
        sampler_for_spec(spec, random.Random(0))


def test_range_check_on_a_text_column_leaves_it_unnarrowed() -> None:
    column = _scalar("name", "text")
    assert _narrow(column, "name > 5") == spec_for_type(column.type)


def test_contradictory_bounds_fail_loudly_on_the_bulk_path() -> None:
    spec = _narrow(_scalar("n", "integer"), "n > 10", "n < 5")
    with pytest.raises(SqlProofGenerationError, match="admit no value"):
        sampler_for_spec(spec, random.Random(0))


def test_excluding_every_remaining_value_fails_loudly_on_the_bulk_path() -> None:
    spec = _narrow(_scalar("flag", "boolean"), "flag NOT IN (true, false)")
    assert spec.excluded_values == (True, False)
    sample = sampler_for_spec(spec, random.Random(0))
    with pytest.raises(SqlProofGenerationError, match="CHECK-excluded"):
        sample()


def test_excluding_every_remaining_value_is_unsatisfiable_on_the_hypothesis_path() -> None:
    column = _scalar("flag", "boolean")
    refined = refine_for_checks(
        column, strategy_for_column(column), (CheckConstraint("flag NOT IN (true, false)"),)
    )
    with pytest.raises(Unsatisfiable):
        refined.example()


def _is_float32(value: float) -> bool:
    return struct.unpack("<f", struct.pack("<f", value))[0] == value


@pytest.mark.parametrize(
    ("check", "holds"),
    [
        # `> 0` must not round down to 0 once stored as float32.
        ("score > 0", lambda v: v > 0),
        ("score < 0", lambda v: v < 0),
        # 0.1 rounds *up* in float32, so the bound must step down.
        ("score < 0.1", lambda v: v < 0.1),
    ],
)
def test_real_column_bounds_hold_after_float32_storage(check: str, holds: Any) -> None:
    column = _scalar("score", "real")
    spec = _narrow(column, check)
    sample = sampler_for_spec(spec, random.Random(7))
    for _ in range(500):
        value = sample()
        assert _is_float32(value) and holds(value), value
    refined = refine_for_checks(column, strategy_for_column(column), (CheckConstraint(check),))

    @SETTINGS
    @given(value=refined)
    def run(value: float) -> None:
        assert _is_float32(value) and holds(value), value

    run()


def test_float32_rounding_helpers_step_off_zero_in_both_directions() -> None:
    smallest = struct.unpack("<f", struct.pack("<I", 1))[0]
    assert float32_ceil(1e-300) == smallest
    assert float32_floor(-1e-300) == -smallest
    assert float32_floor(Decimal("0.1").__float__()) < 0.1
    assert float32_ceil(0.1) >= 0.1
