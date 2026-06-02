"""Range type support (#4, sub-feature 2).

Postgres ships six built-in range types: ``int4range``,
``int8range``, ``numrange``, ``tsrange``, ``tstzrange``,
``daterange``. They're common in scheduling / billing /
inventory schemas (e.g. ``available_during tstzrange``).

Before this change, the parser fell back to scalar text for any
range type name, ``strategy_for_type`` produced random strings,
and Postgres rejected the INSERT with ``invalid input syntax for
type tstzrange``.

After this change, columns typed as a built-in range surface as
``PgType(kind=\"range\", name=\"<range>\", base=PgType(scalar,
<element>))``, and the generator produces psycopg
``Range`` objects with ``lower < upper`` and default ``'[)'``
bounds (matching Postgres's canonical form for discrete-element
ranges like ``int4range`` / ``daterange``).

Invariants pinned down here:

  (i) Columns declared with a built-in range type resolve to
      ``kind=\"range\"`` with the element type as ``base``.
  (ii) Generated values for a range column are psycopg ``Range``
       instances.
  (iii) The lower bound is strictly less than the upper bound
        (no empty or inverted ranges in v1; Postgres accepts
        both shapes but they're rarely what tests want).

Failure cases each invariant addresses:
  (i): Without parser support, ``tstzrange`` is a string token;
       downstream code can't know it's a range or what element
       type to draw.
  (ii): Without ``Range`` generation, the column gets a plain
        string and Postgres rejects the INSERT.
  (iii): A common bug in hand-rolled range generators: drawing
         two independent values and assembling them blindly,
         which gives inverted ranges 50% of the time.
"""

from __future__ import annotations

from datetime import date, datetime

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from psycopg.types.range import Range

from sqlproof.generators.rows import table_rows_strategy
from sqlproof.schema.parse_sql import parse_schema_sql

# ---------------------------------------------------------------------------
# Invariant (i): parser resolves range type names
# ---------------------------------------------------------------------------


def test_parser_resolves_int4range_to_range_kind_with_integer_base() -> None:
    """Invariant (i): int4range surfaces as kind=range, base=integer.
    Failure case: column types like int4range / tstzrange silently
    fell through as scalar text, breaking generation."""
    schema = parse_schema_sql(
        """
        CREATE TABLE bookings (
            id serial PRIMARY KEY,
            quantity int4range NOT NULL
        );
        """
    )
    qty = schema.table("bookings").column("quantity")
    assert qty.type.kind == "range"
    assert qty.type.name == "int4range"
    assert qty.type.base is not None
    assert qty.type.base.name == "integer"


def test_parser_resolves_tstzrange_to_range_kind_with_timestamptz_base() -> None:
    """Invariant (i): all six built-in ranges follow the same
    pattern. This covers the canonical scheduling use case
    (tstzrange for booking windows)."""
    schema = parse_schema_sql(
        """
        CREATE TABLE reservations (
            id serial PRIMARY KEY,
            during tstzrange NOT NULL
        );
        """
    )
    during = schema.table("reservations").column("during")
    assert during.type.kind == "range"
    assert during.type.name == "tstzrange"
    assert during.type.base is not None
    assert during.type.base.name == "timestamptz"


# ---------------------------------------------------------------------------
# Invariant (ii) + (iii): generator produces valid Range objects
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_generator_produces_int4range_with_lower_less_than_upper(
    data: st.DataObject,
) -> None:
    """Invariants (ii) + (iii): every generated value is a Range
    with lower < upper. Failure case: a plain-tuple representation
    would fail Postgres's type check; an inverted range (lower >
    upper) would be technically valid (Postgres normalizes to
    empty) but is almost never what the user wants in a generated
    dataset."""
    schema = parse_schema_sql(
        """
        CREATE TABLE bookings (
            id serial PRIMARY KEY,
            quantity int4range NOT NULL
        );
        """
    )
    bookings = schema.table("bookings")
    rows = data.draw(table_rows_strategy(bookings, count=3))
    for row in rows:
        value = row["quantity"]
        assert isinstance(value, Range), (
            f"int4range column must produce Range, got {type(value).__name__}"
        )
        assert value.lower is not None and value.upper is not None
        assert value.lower < value.upper, (
            f"Lower bound must be less than upper: lower={value.lower}, upper={value.upper}"
        )


@given(data=st.data())
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_generator_produces_tstzrange_with_datetime_endpoints(
    data: st.DataObject,
) -> None:
    """Invariant (ii) for tstzrange: the endpoints must be
    datetime objects (not strings, not None). Failure case: a
    text fallback would put strings into the Range constructor,
    causing psycopg INSERT-side errors that don't point at the
    schema."""
    schema = parse_schema_sql(
        """
        CREATE TABLE reservations (
            id serial PRIMARY KEY,
            during tstzrange NOT NULL
        );
        """
    )
    reservations = schema.table("reservations")
    rows = data.draw(table_rows_strategy(reservations, count=3))
    for row in rows:
        value = row["during"]
        assert isinstance(value, Range)
        assert isinstance(value.lower, datetime)
        assert isinstance(value.upper, datetime)
        assert value.lower < value.upper


@given(data=st.data())
@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_generator_produces_daterange_with_date_endpoints(
    data: st.DataObject,
) -> None:
    """Invariant (ii) for daterange — date-typed endpoints, not
    datetimes. Failure case: a uniform ``st.datetimes`` strategy
    for all range types would put datetime values into daterange,
    which Postgres rejects."""
    schema = parse_schema_sql(
        """
        CREATE TABLE periods (
            id serial PRIMARY KEY,
            span daterange NOT NULL
        );
        """
    )
    periods = schema.table("periods")
    rows = data.draw(table_rows_strategy(periods, count=3))
    for row in rows:
        value = row["span"]
        assert isinstance(value, Range)
        assert isinstance(value.lower, date) and not isinstance(value.lower, datetime)
        assert isinstance(value.upper, date) and not isinstance(value.upper, datetime)
        assert value.lower < value.upper
