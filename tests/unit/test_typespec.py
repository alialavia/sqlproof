from __future__ import annotations

import pytest

from sqlproof.generators.typespec import (
    KNOWN_TYPE_NAMES,
    TYPE_SPEC_BUILDERS,
    spec_for_column,
    spec_for_type,
)
from sqlproof.schema.model import Column, PgType


def test_bigint_spec_carries_int8_bounds():
    spec = spec_for_type(PgType("scalar", "bigint"))
    assert spec.kind == "integer"
    assert spec.min_value == -(2**63)
    assert spec.max_value == 2**63 - 1


def test_varchar_spec_honours_modifier():
    spec = spec_for_type(PgType("scalar", "varchar", modifiers=(40,)))
    assert spec.kind == "text"
    assert spec.max_size == 40


def test_char_spec_is_fixed_width():
    spec = spec_for_type(PgType("scalar", "char", modifiers=(8,)))
    assert spec.min_size == 8
    assert spec.max_size == 8


def test_enum_spec_carries_values():
    spec = spec_for_type(PgType("enum", "mood", enum_values=("sad", "ok")))
    assert spec.kind == "enum"
    assert spec.enum_values == ("sad", "ok")


def test_domain_unwraps_to_base():
    spec = spec_for_type(
        PgType("domain", "positive_int", base=PgType("scalar", "integer"))
    )
    assert spec.kind == "integer"


def test_range_carries_element_spec():
    spec = spec_for_type(PgType("range", "int4range", base=PgType("scalar", "integer")))
    assert spec.kind == "range"
    assert spec.element is not None
    assert spec.element.kind == "integer"


def test_tstzrange_element_is_tz_aware():
    spec = spec_for_type(
        PgType("range", "tstzrange", base=PgType("scalar", "timestamptz"))
    )
    assert spec.element is not None
    assert spec.element.tz_aware is True


def test_vector_requires_dimension():
    from sqlproof.exceptions import SqlProofSchemaError

    with pytest.raises(SqlProofSchemaError):
        spec_for_type(PgType("scalar", "vector"))


def test_unknown_type_falls_back_to_text():
    spec = spec_for_type(PgType("scalar", "some_extension_type"))
    assert spec.kind == "text"


def test_nullable_flag_travels_with_column():
    col = Column("a", PgType("scalar", "integer"), True, None, False)
    spec, nullable = spec_for_column(col)
    assert spec.kind == "integer"
    assert nullable is True


def test_every_registered_name_builds_a_spec():
    for name in KNOWN_TYPE_NAMES:
        builder = TYPE_SPEC_BUILDERS[name]
        modifiers = (8, 2) if name in {"numeric", "decimal"} else (8,)
        assert builder(PgType("scalar", name, modifiers=modifiers)) is not None
