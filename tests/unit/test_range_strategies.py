"""Range edge cases stay reachable without rejecting generated inputs."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from hypothesis import find, given, settings
from hypothesis import strategies as st
from hypothesis.errors import InvalidArgument

from sqlproof.generators.well_known import ranges


@pytest.mark.parametrize(
    "predicate",
    [
        pytest.param(lambda r: r.isempty, id="empty"),
        pytest.param(
            lambda r: not r.isempty and r.lower is not None and r.upper is not None,
            id="bounded",
        ),
        pytest.param(
            lambda r: not r.isempty and r.lower is None and r.upper is not None,
            id="lower-unbounded",
        ),
        pytest.param(
            lambda r: not r.isempty and r.lower is not None and r.upper is None,
            id="upper-unbounded",
        ),
        pytest.param(
            lambda r: not r.isempty and r.lower is None and r.upper is None,
            id="fully-unbounded",
        ),
    ],
)
def test_every_documented_range_shape_is_reachable(predicate) -> None:
    """Oracle: each PostgreSQL range shape must have a discoverable witness."""
    find(
        ranges(st.integers(-10, 10)),
        predicate,
        settings=settings(database=None, deadline=None, max_examples=200),
    )


@given(ranges(st.integers(10, 20)))
def test_finite_range_endpoints_stay_in_the_requested_domain(value) -> None:
    """Oracle: the supplied domain and PostgreSQL's ordered-bound grammar."""
    if value.lower is not None:
        assert 10 <= value.lower <= 20
    if value.upper is not None:
        assert 10 <= value.upper <= 20
    if value.lower is not None and value.upper is not None:
        assert value.lower < value.upper


@given(ranges(st.just(5)))
def test_a_single_endpoint_value_does_not_require_rejection_sampling(value) -> None:
    """A half-open range with equal endpoints is empty; open ends remain valid."""
    assert value.isempty or value.lower is None or value.upper is None


@pytest.mark.parametrize(("first_minute", "second_minute"), [(45, 15), (30, 30)])
def test_timestamp_ranges_order_repeated_hours_by_their_instants(
    fold_timezone, first_minute, second_minute
) -> None:
    """Oracle: UTC distinguishes instants even when wall-clock times repeat."""
    earlier = datetime(2026, 11, 1, 1, first_minute, tzinfo=fold_timezone, fold=0)
    later = datetime(2026, 11, 1, 1, second_minute, tzinfo=fold_timezone, fold=1)
    value = find(
        ranges(st.sampled_from([earlier, later])),
        lambda r: not r.isempty and r.lower is not None and r.upper is not None
        and r.lower.astimezone(UTC) != r.upper.astimezone(UTC),
        settings=settings(database=None, deadline=None),
    )
    assert value.lower.astimezone(UTC) == earlier.astimezone(UTC)
    assert value.upper.astimezone(UTC) == later.astimezone(UTC)


CONTINUOUS = st.decimals(Decimal("-10"), Decimal("10"), places=1)


@pytest.mark.parametrize("bounds", ["[)", "[]", "(]", "()"])
def test_continuous_ranges_reach_every_bound_form(bounds) -> None:
    """Oracle: PostgreSQL's four bound forms are distinct values for a
    continuous subtype, so each needs a bounded, nonempty witness."""
    find(
        ranges(CONTINUOUS),
        lambda r: not r.isempty and None not in (r.lower, r.upper) and r.bounds == bounds,
        settings=settings(database=None, deadline=None, max_examples=500),
    )


@pytest.mark.parametrize(
    ("predicate", "label"),
    [
        (lambda r: not r.isempty and r.lower is not None and r.lower == r.upper, "point"),
        (lambda r: r.lower is None and r.upper is not None and r.bounds == "(]", "upper inclusive"),
        (lambda r: r.lower is None and r.upper is not None and r.bounds == "()", "upper exclusive"),
        (lambda r: r.lower is not None and r.upper is None and r.bounds == "[)", "lower inclusive"),
        (lambda r: r.lower is not None and r.upper is None and r.bounds == "()", "lower exclusive"),
    ],
)
def test_continuous_ranges_reach_points_and_both_kinds_of_open_end(predicate, label) -> None:
    """Oracle: `[x,x]` is a nonempty single point, and a half-unbounded
    range's finite end may be inclusive or exclusive."""
    del label
    find(
        ranges(CONTINUOUS),
        predicate,
        settings=settings(database=None, deadline=None, max_examples=500),
    )


@given(ranges(CONTINUOUS))
def test_continuous_endpoints_are_ordered_and_equal_only_for_points(value) -> None:
    """Oracle: PostgreSQL rejects lower > upper, and equal endpoints are a
    nonempty range only in the closed `[]` form."""
    if value.isempty or value.lower is None or value.upper is None:
        return
    assert value.lower <= value.upper
    if value.lower == value.upper:
        assert value.bounds == "[]"


@pytest.mark.parametrize(
    "elements",
    [st.integers(-5, 5), st.dates(date(2026, 1, 1), date(2026, 1, 5))],
    ids=["integers", "dates"],
)
@given(data=st.data())
def test_discrete_ranges_use_the_form_postgres_stores(elements, data) -> None:
    """Oracle: PostgreSQL canonicalizes int4range, int8range and daterange
    to `[)`, so anything else would read back differently from what was
    generated."""
    value = data.draw(ranges(elements))
    if value.isempty:
        return
    lower = "(" if value.lower is None else "["
    upper = ")"
    assert value.bounds == lower + upper


@pytest.mark.parametrize(
    "elements",
    [st.just(Decimal("NaN")), st.just(float("nan")), st.just(Decimal("-sNaN"))],
    ids=["decimal", "float", "signalling"],
)
def test_nan_endpoints_are_refused_with_a_clear_error(elements) -> None:
    """Partial function: NaN cannot bound a range as written, so the
    strategy refuses it instead of producing invalid or misordered input."""

    @settings(database=None, deadline=None, max_examples=5)
    @given(ranges(elements))
    def draw(_value) -> None:
        pass

    with pytest.raises(InvalidArgument, match="cannot be NaN"):
        draw()
