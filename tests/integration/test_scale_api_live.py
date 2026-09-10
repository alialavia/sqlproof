"""The public surface, used the way a caller would.

The output is an ASSERTION, not a report: CI goes red when a function's
buffer work grows faster than the test allows. The artifact written
alongside is for trend history, the way mutation runs are. The sweep is
also destructive to the tables it models, which the last three tests
pin (Ruling AM).
"""
from __future__ import annotations

import json
import os
from itertools import pairwise

import psycopg
import pytest

from sqlproof import SqlProof
from sqlproof.config import SqlProofConfig
from sqlproof.exceptions import SqlProofUsageError
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
    # Ruling AO: the artifact says how the argument was chosen -- the
    # largest key value, by `heaviest` -- and claims no worst case.
    (artifact,) = tmp_path.glob("*.json")
    data = json.loads(artifact.read_text())
    assert data["argument_policy"] == [{"kind": "heaviest", "column": "api_test.orgs.id"}]


# --- Ruling AM: the sweep empties and repopulates every modelled table ---


def _row_counts() -> dict[str, int]:
    """Read on a fresh connection, so nothing the sweep's own session
    left uncommitted can make a count look right."""
    with psycopg.connect(os.environ[DSN_ENV]) as fresh:
        return {
            name: fresh.execute(f"SELECT count(*) FROM api_test.{name}").fetchone()[0]
            for name in ("orgs", "events")
        }


def _seed_three_orgs() -> None:
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        connection.execute(
            "INSERT INTO api_test.orgs VALUES (1, 'kept'), (2, 'kept'), (3, 'kept')"
        )


def test_a_modelled_table_holding_rows_is_refused_and_left_untouched(proof):
    """C1: `scale_analysis` used to TRUNCATE the user's own tables and
    leave synthetic rows behind. A table that already holds rows is now
    refused, and nothing is touched."""
    _seed_three_orgs()
    with pytest.raises(SqlProofUsageError, match=r"api_test\.orgs \(3 rows\)") as exc_info:
        scale_analysis(
            proof,
            "api_test.events_for",
            sizes={"orgs": 20, "events": 400},
            args=[heaviest("api_test.orgs.id")],
            artifact_dir=None,
        )
    assert "truncate_existing=True" in str(exc_info.value)
    assert _row_counts() == {"orgs": 3, "events": 0}


def test_truncate_existing_runs_the_sweep_and_leaves_every_table_empty(proof):
    _seed_three_orgs()
    result = scale_analysis(
        proof,
        "api_test.events_for",
        sizes={"orgs": 20, "events": 400},
        args=[heaviest("api_test.orgs.id")],
        max_factor=16,
        artifact_dir=None,
        truncate_existing=True,
    )
    assert len(result.points) >= 5
    assert 0.7 < result.exponent < 1.3
    assert _row_counts() == {"orgs": 0, "events": 0}


def test_a_function_that_raises_mid_sweep_propagates_and_leaves_tables_empty(proof):
    """The function fails at factor 4, once 1,600 events are loaded (the
    error message carries the count, proving the table was full when it
    failed). The function's own error must reach the caller, and the
    closing TRUNCATE must still leave every modelled table empty."""
    with psycopg.connect(os.environ[DSN_ENV], autocommit=True) as connection:
        connection.execute(
            "CREATE FUNCTION api_test.fails_when_large() RETURNS bigint "
            "LANGUAGE plpgsql STABLE AS $$ DECLARE n bigint; BEGIN "
            "SELECT count(*) INTO n FROM api_test.events; "
            "IF n > 1000 THEN RAISE EXCEPTION 'sqlproof test: % events', n; END IF; "
            "RETURN n; END $$"
        )
    with pytest.raises(psycopg.errors.RaiseException, match="sqlproof test: 1600 events"):
        scale_analysis(
            proof,
            "api_test.fails_when_large",
            sizes={"orgs": 20, "events": 400},
            artifact_dir=None,
        )
    assert _row_counts() == {"orgs": 0, "events": 0}
