"""PostgreSQL is the oracle for the documented column override recipes."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from sqlproof import SqlProof, postgres
from sqlproof.config import SqlProofConfig

pytestmark = pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)

PROPERTY = settings(
    max_examples=20,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture
def edge_proof():
    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    name = f"sqlproof_edges_{uuid4().hex}"
    with postgres.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
        try:
            conn.execute(f"""
                CREATE TABLE "{name}".events (
                    id integer PRIMARY KEY,
                    day date NOT NULL,
                    occurred_at timestamptz NOT NULL,
                    adjustment interval NOT NULL,
                    tags integer[] NOT NULL,
                    payload jsonb
                )
            """)
            yield SqlProof.from_config(SqlProofConfig(connection_string=dsn, schema=name))
        finally:
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')


@PROPERTY
@given(data=st.data())
def test_overrides_preserve_dates_outside_the_default_window(edge_proof, data):
    dataset = data.draw(edge_proof.dataset_strategy(
        sizes={"events": 1},
        columns={"events.day": st.sampled_from([date(1900, 1, 1), date(2100, 12, 31)])},
    ))
    with edge_proof.client_for_dataset(dataset) as db:
        actual = db.scalar(f'SELECT day FROM "{edge_proof.config.schema}".events')
    assert actual == dataset["events"][0]["day"]


@PROPERTY
@given(data=st.data())
def test_overrides_preserve_negative_intervals(edge_proof, data):
    dataset = data.draw(edge_proof.dataset_strategy(
        sizes={"events": 1},
        columns={"events.adjustment": st.timedeltas(
            timedelta(days=-365), timedelta(microseconds=-1),
        )},
    ))
    with edge_proof.client_for_dataset(dataset) as db:
        actual = db.scalar(f'SELECT adjustment FROM "{edge_proof.config.schema}".events')
    assert actual == dataset["events"][0]["adjustment"]


@PROPERTY
@given(data=st.data())
def test_overrides_preserve_both_instants_of_a_repeated_hour(edge_proof, data):
    zone = ZoneInfo("America/New_York")
    instants = [datetime(2024, 11, 3, 1, 30, tzinfo=zone, fold=fold) for fold in (0, 1)]
    dataset = data.draw(edge_proof.dataset_strategy(
        sizes={"events": 2},
        columns={"events.occurred_at": lambda context: instants[context.row_index]},
    ))
    with edge_proof.client_for_dataset(dataset) as db:
        rows = db.query(f'SELECT occurred_at FROM "{edge_proof.config.schema}".events ORDER BY id')
    assert [row["occurred_at"].astimezone(UTC) for row in rows] == [
        instant.astimezone(UTC) for instant in instants
    ]


@PROPERTY
@given(data=st.data())
def test_overrides_preserve_null_elements_in_long_arrays(edge_proof, data):
    dataset = data.draw(edge_proof.dataset_strategy(
        sizes={"events": 1},
        columns={"events.tags": st.lists(st.integers(-100, 100), min_size=5, max_size=8).map(
            lambda tail: [None, *tail]
        )},
    ))
    with edge_proof.client_for_dataset(dataset) as db:
        actual = db.scalar(f'SELECT tags FROM "{edge_proof.config.schema}".events')
    assert actual == dataset["events"][0]["tags"]


@PROPERTY
@given(data=st.data())
def test_overrides_distinguish_sql_null_json_null_and_exact_integers(edge_proof, data):
    dataset = data.draw(edge_proof.dataset_strategy(
        sizes={"events": 3},
        columns={"events.payload": lambda context: (
            None, postgres.Jsonb(None), 2**63 - 1
        )[context.row_index]},
    ))
    with edge_proof.client_for_dataset(dataset) as db:
        rows = db.query(
            f'SELECT payload, payload IS NULL AS sql_null, jsonb_typeof(payload) AS kind '
            f'FROM "{edge_proof.config.schema}".events ORDER BY id'
        )
    assert rows == [
        {"payload": None, "sql_null": True, "kind": None},
        {"payload": None, "sql_null": False, "kind": "null"},
        {"payload": 2**63 - 1, "sql_null": False, "kind": "number"},
    ]
