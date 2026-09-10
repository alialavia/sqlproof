"""The public surface, used the way a caller would.

The output is an ASSERTION, not a report: CI goes red when someone
writes a query that will not survive growth. The artifact written
alongside is for trend history, the way mutation runs are.
"""
from __future__ import annotations

import os
from itertools import pairwise

import psycopg
import pytest

from sqlproof import SqlProof
from sqlproof.config import SqlProofConfig
from sqlproof.scale import heaviest, scale_analysis

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE TABLE orgs (id bigint PRIMARY KEY, name text NOT NULL);
CREATE TABLE events (
  id bigint PRIMARY KEY,
  org_id bigint NOT NULL REFERENCES orgs(id),
  tag text NOT NULL
);
"""


@pytest.fixture
def proof():
    dsn = os.environ[DSN_ENV]
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS api_test CASCADE")
        try:
            # Ruling Z: setup lives inside the try too, so a failure partway
            # through (e.g. the function DDL) still drops whatever
            # schema/tables did get created, instead of leaking them past
            # this test.
            connection.execute("CREATE SCHEMA api_test")
            connection.execute("SET search_path TO api_test")
            connection.execute(SCHEMA_SQL)
            connection.execute(
                "CREATE FUNCTION api_test.events_for(o bigint) RETURNS bigint "
                "LANGUAGE sql STABLE AS "
                "$$ SELECT count(*) FROM api_test.events WHERE org_id = o $$"
            )
            yield SqlProof.from_config(
                SqlProofConfig(connection_string=dsn, schema="api_test")
            )
        finally:
            connection.execute("DROP SCHEMA IF EXISTS api_test CASCADE")


def test_scale_analysis_reads_as_a_test(proof, tmp_path):
    result = scale_analysis(
        proof,
        "api_test.events_for",
        sizes={"orgs": 20, "events": 400},
        args=[heaviest("api_test.orgs.id")],
        max_factor=16,
        artifact_dir=tmp_path,
    )
    # events_for is linear by construction: no index on events.org_id,
    # so the count is a full seq scan.
    assert 0.7 < result.exponent < 1.3
    assert not result.spills_below(1_000)


def test_resolved_arguments_are_recorded_per_point(proof, tmp_path):
    """A surprising result has to be reproducible, and that means
    knowing which argument each point was measured with."""
    result = scale_analysis(
        proof,
        "api_test.events_for",
        sizes={"orgs": 20, "events": 400},
        args=[heaviest("api_test.orgs.id")],
        max_factor=8,
        artifact_dir=tmp_path,
    )
    # Pins that max_factor=8 actually reached run_sweep through **kwargs
    # (with the default 32 the sweep stops at 16 on fit quality).
    assert max(p.factor for p in result.points) == 8
    assert all(len(p.args) == 1 for p in result.points)
    # The heaviest key grows with the data, so it must strictly increase
    # with factor (points arrive in ascending factor order).
    resolved_values = [p.args[0] for p in result.points]
    assert all(a < b for a, b in pairwise(resolved_values))
