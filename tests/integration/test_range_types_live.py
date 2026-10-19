"""Live-Postgres integration test for range types (#4b).

Confirms that:
  (a) ``introspect_schema`` against a real ``pg_type`` row for
      a range column resolves to ``kind="range"`` with the
      element type as ``base``.
  (b) The generator's ``Range`` objects round-trip through
      database's adapter into Postgres without rejection.

Without these, the unit tests' parser invariant could be correct
in isolation while the introspect side or the wire-format adapter
silently broke real-world usage.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from hypothesis import find, given, settings
from hypothesis import strategies as st

from sqlproof import _database as database
from sqlproof.generators.graph import dataset_strategy
from sqlproof.generators.well_known import ranges
from sqlproof.scale.load import copy_table
from sqlproof.schema.introspect import introspect_schema
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA_SQL = """
CREATE TABLE bookings (
    id serial PRIMARY KEY,
    during tstzrange NOT NULL,
    quantity int4range NOT NULL
);
"""


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
@pytest.mark.parametrize(
    ("range_type", "elements"),
    [
        ("int4range", st.integers(-1000, 1000)),
        ("int8range", st.integers(-(2**40), 2**40)),
        ("numrange", st.decimals(Decimal("-1000"), Decimal("1000"), places=2)),
        ("daterange", st.dates(date(2000, 1, 1), date(2030, 1, 1))),
        ("tsrange", st.datetimes(datetime(2000, 1, 1), datetime(2030, 1, 1))),
        (
            "tstzrange",
            st.datetimes(datetime(2000, 1, 1), datetime(2030, 1, 1), timezones=st.just(UTC)),
        ),
    ],
)
@settings(max_examples=60, deadline=None)
@given(data=st.data())
def test_range_overrides_preserve_empty_and_unbounded_values_in_postgres(
    range_type, elements, data
) -> None:
    """Oracle: PostgreSQL's range accessors after loading a generated dataset."""
    schema = parse_schema_sql(
        f"CREATE TABLE range_cases (span {range_type} NOT NULL)", schema="pg_temp"
    )
    dataset = data.draw(
        dataset_strategy(
            schema, sizes={"range_cases": 1}, columns={"range_cases.span": ranges(elements)}
        )
    )
    value = dataset["range_cases"][0]["span"]
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as connection:
        connection.execute(f"CREATE TEMP TABLE range_cases (span {range_type} NOT NULL)")
        copy_table(connection, schema.tables[0], dataset["range_cases"], ["span"])
        empty, lower_inf, upper_inf, lower, upper, lower_inc, upper_inc = connection.execute(
            "SELECT isempty(span), lower_inf(span), upper_inf(span), lower(span), upper(span), "
            "lower_inc(span), upper_inc(span) FROM range_cases"
        ).fetchone()
    assert empty == value.isempty
    if not empty:
        assert (lower_inf, upper_inf) == (value.lower is None, value.upper is None)
        assert (lower, upper) == (value.lower, value.upper)
        if value.lower is not None:
            assert lower_inc == (value.bounds[0] == "[")
        if value.upper is not None:
            assert upper_inc == (value.bounds[1] == "]")


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
@pytest.mark.parametrize(("first_minute", "second_minute"), [(45, 15), (30, 30)])
def test_repeated_hour_ranges_preserve_their_instants_in_postgres(
    fold_timezone, first_minute, second_minute
) -> None:
    """Oracle: PostgreSQL orders timestamptz bounds by their UTC instants."""
    earlier = datetime(2026, 11, 1, 1, first_minute, tzinfo=fold_timezone, fold=0)
    later = datetime(2026, 11, 1, 1, second_minute, tzinfo=fold_timezone, fold=1)
    value = find(
        ranges(st.sampled_from([earlier, later])),
        lambda r: not r.isempty and r.lower is not None and r.upper is not None
        and r.lower.astimezone(UTC) != r.upper.astimezone(UTC),
        settings=settings(database=None, deadline=None),
    )
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as connection:
        stored = connection.execute(
            "SELECT lower(r), upper(r) FROM (SELECT %s::tstzrange AS r) ranges", (value,)
        ).fetchone()
    assert stored == (earlier.astimezone(UTC), later.astimezone(UTC))


@pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)
def test_range_typed_columns_round_trip_through_postgres() -> None:
    """Failure case: int4range / tstzrange columns generate as
    text fallback, and the INSERT batch errors with `invalid
    input syntax for type tstzrange` — surfacing the parser-side
    or introspector-side gap."""
    from sqlproof import SqlProof
    from sqlproof.config import SqlProofConfig

    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    schema_name = f"sqlproof_range_{uuid4().hex}"
    with database.connect(dsn, autocommit=True) as connection:
        connection.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            connection.execute(f'SET search_path TO "{schema_name}"')
            for statement in SCHEMA_SQL.strip().split(";"):
                if statement.strip():
                    connection.execute(statement)

            # Parser-side
            parsed = parse_schema_sql(SCHEMA_SQL).table("bookings")
            assert parsed.column("during").type.kind == "range"
            assert parsed.column("quantity").type.kind == "range"

            # Introspector-side
            with connection.cursor(row_factory=database.dict_row) as cur:
                introspected = introspect_schema(cur, schema=schema_name).table(
                    "bookings", schema=schema_name
                )
            during_type = introspected.column("during").type
            assert during_type.kind == "range"
            assert during_type.name == "tstzrange"

            # End-to-end: generate a dataset and let database insert
            # the Range objects into Postgres. If anything in the
            # adapter chain breaks, check() raises before the property
            # body runs.
            proof = SqlProof.from_config(
                SqlProofConfig(connection_string=dsn, schema=schema_name)
            )

            def property_check(db) -> None:
                # Schema-qualified — db.query doesn't always inherit
                # the dataset's search_path across connection reuse.
                rows = db.query(
                    f'SELECT during, quantity FROM "{schema_name}".bookings'
                )
                assert all(row["during"] is not None for row in rows)
                assert all(row["quantity"] is not None for row in rows)

            proof.check(
                "range-typed columns round-trip cleanly",
                sizes={"bookings": 4},
                property=property_check,
                runs=2,
            )
        finally:
            connection.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
