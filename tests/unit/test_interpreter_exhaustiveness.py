"""Both generation paths must interpret every registered type.

This is the backstop for the structural guarantee in typespec.py: a
type added to the registry but unhandled by one interpreter fails here,
in CI, rather than on a user's schema.
"""
from __future__ import annotations

import random

import pytest

from sqlproof.generators.bulk import sampler_for_spec
from sqlproof.generators.columns import strategy_for_spec
from sqlproof.generators.typespec import (
    KNOWN_TYPE_NAMES,
    TYPE_SPEC_BUILDERS,
    spec_for_type,
)
from sqlproof.schema.model import PgType


# vector legitimately requires a modifier; give every builder one that
# satisfies it. numeric/decimal read modifiers[1] for scale.
def _pg_type(name: str) -> PgType:
    modifiers = (10, 2) if name in {"numeric", "decimal"} else (8,)
    return PgType("scalar", name, modifiers=modifiers)


@pytest.mark.parametrize("name", sorted(KNOWN_TYPE_NAMES))
def test_hypothesis_interpreter_handles_every_registered_type(name):
    spec = TYPE_SPEC_BUILDERS[name](_pg_type(name))
    assert strategy_for_spec(spec) is not None


@pytest.mark.parametrize("name", sorted(KNOWN_TYPE_NAMES))
def test_bulk_interpreter_handles_every_registered_type(name):
    spec = TYPE_SPEC_BUILDERS[name](_pg_type(name))
    sample = sampler_for_spec(spec, random.Random(0))
    sample()  # must not raise


@pytest.mark.parametrize(
    "pg_type",
    [
        PgType("enum", "mood", enum_values=("a", "b")),
        PgType("domain", "d", base=PgType("scalar", "integer")),
        PgType("range", "int4range", base=PgType("scalar", "integer")),
        PgType(
            "composite",
            "addr",
            composite_fields=(("city", PgType("scalar", "text")),),
        ),
    ],
    ids=["enum", "domain", "range", "composite"],
)
def test_both_interpreters_handle_every_type_kind(pg_type):
    spec = spec_for_type(pg_type)
    assert strategy_for_spec(spec) is not None
    sampler_for_spec(spec, random.Random(0))()
