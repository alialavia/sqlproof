from __future__ import annotations

import random
import signal
from decimal import Decimal

import pytest

from sqlproof.exceptions import SqlProofGenerationError
from sqlproof.generators.bulk import sampler_for_column, sampler_for_spec
from sqlproof.generators.typespec import TypeSpec, spec_for_type
from sqlproof.schema.model import Column, PgType


def test_integer_sampler_respects_bounds():
    spec = spec_for_type(PgType("scalar", "smallint"))
    sample = sampler_for_spec(spec, random.Random(1))
    for _ in range(200):
        assert -32_768 <= sample() <= 32_767


def test_text_sampler_respects_max_size_and_excludes_nul():
    spec = spec_for_type(PgType("scalar", "varchar", modifiers=(10,)))
    sample = sampler_for_spec(spec, random.Random(1))
    for _ in range(200):
        v = sample()
        assert len(v) <= 10
        assert "\x00" not in v


def test_char_sampler_is_exactly_fixed_width():
    spec = spec_for_type(PgType("scalar", "char", modifiers=(6,)))
    sample = sampler_for_spec(spec, random.Random(1))
    assert all(len(sample()) == 6 for _ in range(50))


def test_numeric_sampler_respects_scale():
    spec = spec_for_type(PgType("scalar", "numeric", modifiers=(10, 3)))
    sample = sampler_for_spec(spec, random.Random(1))
    v = sample()
    assert isinstance(v, Decimal)
    assert -v.as_tuple().exponent == 3


def test_enum_sampler_only_emits_declared_values():
    spec = spec_for_type(PgType("enum", "mood", enum_values=("sad", "ok", "great")))
    sample = sampler_for_spec(spec, random.Random(1))
    assert {sample() for _ in range(100)} <= {"sad", "ok", "great"}


def test_same_seed_produces_identical_sequences():
    spec = spec_for_type(PgType("scalar", "bigint"))
    a = [sampler_for_spec(spec, random.Random(7))() for _ in range(5)]
    b = [sampler_for_spec(spec, random.Random(7))() for _ in range(5)]
    assert a == b


def test_nullable_column_produces_nulls_at_roughly_the_configured_rate():
    col = Column("a", PgType("scalar", "integer"), True, None, False)
    sample = sampler_for_column(col, random.Random(3), null_frac=0.25)
    values = [sample() for _ in range(4000)]
    observed = values.count(None) / len(values)
    assert 0.20 < observed < 0.30


def test_non_nullable_column_never_produces_null():
    col = Column("a", PgType("scalar", "integer"), False, None, False)
    sample = sampler_for_column(col, random.Random(3), null_frac=0.9)
    assert all(sample() is not None for _ in range(500))


def test_sampler_for_column_uses_override_spec_instead_of_deriving_one():
    # The column's own type is integer, but a caller who has already
    # narrowed a spec (e.g. via CHECK-constraint refinement) can pass
    # it directly rather than have sampler_for_column re-derive one
    # from the column's declared type.
    col = Column("a", PgType("scalar", "integer"), False, None, False)
    override = TypeSpec(kind="boolean")
    sample = sampler_for_column(col, random.Random(1), spec=override)
    assert all(isinstance(sample(), bool) for _ in range(50))


def test_sampler_for_column_override_spec_still_respects_column_nullability():
    # Nullability always comes from the column, even when the spec is
    # overridden -- only the value shape is replaced.
    col = Column("a", PgType("scalar", "integer"), True, None, False)
    override = TypeSpec(kind="boolean")
    sample = sampler_for_column(col, random.Random(2), null_frac=0.3, spec=override)
    values = [sample() for _ in range(3000)]
    observed = values.count(None) / len(values)
    assert 0.24 < observed < 0.36
    assert all(v is None or isinstance(v, bool) for v in values)


def test_range_sampler_raises_instead_of_hanging_on_a_single_value_domain():
    # Regression: a range whose element domain has exactly one
    # possible value (e.g. a single-value enum) can never produce two
    # distinct draws, so the retry loop must give up loudly rather
    # than spin forever. A SIGALRM guard bounds this test itself so a
    # future regression to an unbounded loop fails fast instead of
    # hanging the suite.
    spec = TypeSpec(kind="range", element=TypeSpec(kind="enum", enum_values=("only",)))
    sample = sampler_for_spec(spec, random.Random(1))

    def _on_alarm(signum: int, frame: object) -> None:
        raise TimeoutError("range sampler did not raise within the timeout guard")

    previous_handler = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(5)
    try:
        with pytest.raises(SqlProofGenerationError):
            sample()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_handler)
