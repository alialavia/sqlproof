"""Range edge cases stay reachable without rejecting generated inputs."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import find, given, settings
from hypothesis import strategies as st

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
        lambda r: not r.isempty and r.lower is not None and r.upper is not None,
        settings=settings(database=None, deadline=None),
    )
    assert value.lower.astimezone(UTC) == earlier.astimezone(UTC)
    assert value.upper.astimezone(UTC) == later.astimezone(UTC)
