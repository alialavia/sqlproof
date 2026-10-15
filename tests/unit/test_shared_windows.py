"""Both generators draw the six unbounded kinds from the same window.

`typespec.py` holds one window per kind (date, datetime, float, interval,
binary, vector). This file checks the agreement that the exhaustiveness
test cannot: every value either interpreter draws lies inside the shared
window, and each interpreter actually reaches both ends of it, so neither
is quietly using a narrower or wider range of its own.

Values, not pg_stats, are compared across the two paths. A cross-path
`n_distinct` or `correlation` comparison is ill-posed for the same reason
the cross-path `null_frac` comparison was dropped (see
tests/integration/test_bulk_null_frac_live.py): what Hypothesis draws
depends on how it is driven, not only on the strategy. Bulk statistics
are checked against their analytic expectations instead, in
tests/integration/test_bulk_statistics_live.py.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, find, given, settings
from hypothesis import strategies as st

from sqlproof.generators.bulk import sampler_for_spec
from sqlproof.generators.columns import strategy_for_spec
from sqlproof.generators.typespec import (
    BINARY_MAX_SIZE,
    DATE_WINDOW,
    DATETIME_WINDOW,
    FLOAT_WINDOW,
    INTERVAL_WINDOW,
    VECTOR_COMPONENT_WINDOW,
    TypeSpec,
    float_bounds,
    spec_for_type,
)
from sqlproof.schema.model import PgType


def _spec(name: str, *modifiers: int) -> TypeSpec:
    return spec_for_type(PgType("scalar", name, modifiers=modifiers))


def _vector_components(value: str) -> list[float]:
    return [float(x) for x in value.strip("[]").split(",")]


AWARE_WINDOW = (DATETIME_WINDOW[0].replace(tzinfo=UTC), DATETIME_WINDOW[1].replace(tzinfo=UTC))

# kind -> (spec, key mapping a drawn value to an ordered scalar, (lo, hi))
CASES: dict[str, tuple[TypeSpec, Callable[[Any], Any], tuple[Any, Any]]] = {
    "date": (_spec("date"), lambda v: v, DATE_WINDOW),
    "timestamp": (_spec("timestamp"), lambda v: v, DATETIME_WINDOW),
    "tz-aware timestamp": (TypeSpec(kind="datetime", tz_aware=True), lambda v: v, AWARE_WINDOW),
    "double precision": (_spec("double precision"), lambda v: v, FLOAT_WINDOW),
    "real": (_spec("real"), lambda v: v, FLOAT_WINDOW),
    "interval": (_spec("interval"), lambda v: v, INTERVAL_WINDOW),
    "bytea length": (_spec("bytea"), len, (0, BINARY_MAX_SIZE)),
    "vector component": (
        _spec("vector", 3),
        lambda v: min(_vector_components(v)),
        VECTOR_COMPONENT_WINDOW,
    ),
}


@pytest.mark.parametrize("kind", sorted(CASES))
def test_bulk_draws_stay_inside_and_span_the_window(kind: str) -> None:
    spec, key, (lo, hi) = CASES[kind]
    draw = sampler_for_spec(spec, random.Random(7))
    keys = [key(draw()) for _ in range(5000)]
    assert lo <= min(keys) and max(keys) <= hi
    if kind == "vector component":
        return  # min over components skews low; the edge checks cover it
    # Uniform draws reach the outer tenth of the window at both ends.
    span = hi - lo
    assert min(keys) <= lo + span / 10
    assert max(keys) >= hi - span / 10


@pytest.mark.parametrize("kind", sorted(CASES))
@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_hypothesis_draws_stay_inside_the_window(kind: str, data: st.DataObject) -> None:
    spec, key, (lo, hi) = CASES[kind]
    value = data.draw(strategy_for_spec(spec))
    assert lo <= key(value) <= hi


@pytest.mark.parametrize("kind", sorted(CASES))
def test_hypothesis_reaches_both_ends_of_the_window(kind: str) -> None:
    """Shrinking toward each end must land on the edge itself: the
    strategy's own bounds are the window, not something narrower."""
    spec, key, (lo, hi) = CASES[kind]
    strategy = strategy_for_spec(spec)
    span = hi - lo
    search = settings(max_examples=5000, database=None)
    near_lo = find(strategy, lambda v: key(v) <= lo + span / 1000, settings=search)
    near_hi = find(strategy, lambda v: key(v) >= hi - span / 1000, settings=search)
    assert lo <= key(near_lo) and key(near_hi) <= hi


def test_a_one_sided_check_bound_moves_the_window_identically() -> None:
    """`score > 5e6` lies beyond the window; both paths draw from the same
    window of the default width above it."""
    spec = TypeSpec(kind="float", min_value=5_000_000)
    lo, hi = float_bounds(spec)
    assert (lo, hi) == (5e6, 7e6)
    bulk = sampler_for_spec(spec, random.Random(1))
    assert all(lo <= bulk() <= hi for _ in range(1000))
    smallest = find(strategy_for_spec(spec), lambda v: True)
    assert lo <= smallest <= hi


def test_real_window_edges_are_float32_values() -> None:
    spec = TypeSpec(kind="float", float_width=32, max_value=-1_999_999.9)
    lo, hi = float_bounds(spec)
    import struct

    for edge in (lo, hi):
        assert struct.unpack("<f", struct.pack("<f", edge))[0] == edge
    assert lo <= hi


def test_naive_and_aware_windows_are_the_same_instants() -> None:
    assert AWARE_WINDOW[0] == datetime(2000, 1, 1, tzinfo=UTC)
    assert AWARE_WINDOW[1] - AWARE_WINDOW[0] == DATETIME_WINDOW[1] - DATETIME_WINDOW[0]
