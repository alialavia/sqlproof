"""A failing property's counterexample, reloaded as a scenario.

The whole loop a person runs: a property fails and writes its
counterexample, `sqlproof replay FILE --as-scenario` turns it into a
scenario, and the scenario loads the same rows (with new keys, the same
relationships) into the database.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sqlproof import SqlProof
from sqlproof import _database as database
from sqlproof.cli import main
from sqlproof.config import SqlProofConfig
from sqlproof.exceptions import SqlProofPropertyFailure
from sqlproof.reporter.json_io import _json_default  # pyright: ignore[reportPrivateUsage]
from sqlproof.runners.property import sqlproof
from sqlproof.scenarios.serialize import load_file

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"
SCHEMA = "replay_live"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = f"""
CREATE TYPE {SCHEMA}.mood AS ENUM ('calm', 'cross');
CREATE TABLE {SCHEMA}.customers (
  id bigint PRIMARY KEY,
  tier int NOT NULL CHECK (tier BETWEEN 1 AND 3),
  meta jsonb NOT NULL,
  wait interval NOT NULL,
  blob bytea NOT NULL,
  mood {SCHEMA}.mood NOT NULL,
  seen_at timestamptz NOT NULL,
  ratio double precision NOT NULL
);
CREATE TABLE {SCHEMA}.teams (id bigint PRIMARY KEY, name text NOT NULL);
CREATE TABLE {SCHEMA}.memberships (
  customer_id bigint REFERENCES {SCHEMA}.customers(id),
  team_id bigint REFERENCES {SCHEMA}.teams(id),
  role text NOT NULL,
  PRIMARY KEY (customer_id, team_id)
);
CREATE TABLE {SCHEMA}.invoices (
  id bigint PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES {SCHEMA}.customers(id),
  total numeric(10, 2) NOT NULL
);
"""

SIZES = {"customers": 2, "teams": 2, "memberships": 2, "invoices": 3}


@pytest.fixture
def proof() -> Any:
    dsn = os.environ[DSN_ENV]
    with database.connect(dsn, autocommit=True) as connection:
        connection.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        try:
            connection.execute(f"CREATE SCHEMA {SCHEMA}")
            connection.execute(SCHEMA_SQL)
            yield SqlProof.from_config(SqlProofConfig(connection_string=dsn, schema=SCHEMA))
        finally:
            connection.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")


def _fail(proof: Any, failure_dir: Path, seed: int) -> dict[str, Any]:
    @sqlproof(proof, sizes=SIZES, runs=5, seed=seed, failure_dir=failure_dir)
    def totals_are_never_seen(db: Any) -> None:
        rows = db.query(f"SELECT count(*) AS n FROM {SCHEMA}.invoices")
        assert rows[0]["n"] == 0, "invoices exist"

    with pytest.raises(SqlProofPropertyFailure):
        totals_are_never_seen()
    payload: dict[str, Any] = json.loads(
        (failure_dir / "totals_are_never_seen.json").read_text(encoding="utf-8")
    )
    return payload


def test_the_seed_is_recorded_and_reproduces_the_counterexample(proof: Any, tmp_path: Path) -> None:
    first = _fail(proof, tmp_path / "a", seed=20261014)
    second = _fail(proof, tmp_path / "b", seed=20261014)
    assert first["seed"] == second["seed"] == 20261014
    assert first["dataset"] == second["dataset"]


def test_a_counterexample_reloads_as_a_scenario(proof: Any, tmp_path: Path) -> None:
    dsn = os.environ[DSN_ENV]
    payload = _fail(proof, tmp_path, seed=7)
    output = tmp_path / "failure.scenario.yaml"
    code = main(
        [
            "replay",
            str(tmp_path / "totals_are_never_seen.json"),
            "--as-scenario",
            "--dsn",
            dsn,
            "--schema",
            SCHEMA,
            "--output",
            str(output),
        ]
    )
    assert code == 0
    report = load_file(output).load(dsn, schema=SCHEMA)
    assert report.rows == {f"{SCHEMA}.{t}": n for t, n in SIZES.items()}

    loaded = _rows(dsn)
    expected = payload["dataset"]
    keys = {
        table: {
            old["id"]: new["id"] for old, new in zip(expected[table], loaded[table], strict=True)
        }
        for table in ("customers", "teams")
    }
    for old, new in zip(expected["customers"], loaded["customers"], strict=True):
        for column in ("tier", "meta", "blob", "mood", "ratio"):
            assert new[column] == old[column], column
        assert new["wait"] == pytest.approx(old["wait"])
        # Plain timestamptz draws are naive; the test database runs in UTC.
        assert _instant(new["seen_at"]) == _instant(old["seen_at"])
    assert [
        (keys["customers"][m["customer_id"]], keys["teams"][m["team_id"]], m["role"])
        for m in expected["memberships"]
    ] == [(m["customer_id"], m["team_id"], m["role"]) for m in loaded["memberships"]]
    assert [(keys["customers"][i["customer_id"]], i["total"]) for i in expected["invoices"]] == [
        (i["customer_id"], i["total"]) for i in loaded["invoices"]
    ]


def _rows(dsn: str) -> dict[str, list[dict[str, Any]]]:
    """Every table's rows in load order, as the counterexample file writes
    values."""
    out: dict[str, list[dict[str, Any]]] = {}
    with database.connect(dsn, row_factory=database.dict_row) as connection:
        for table, order in (
            ("customers", "id"),
            ("teams", "id"),
            ("memberships", "ctid"),
            ("invoices", "id"),
        ):
            rows = connection.execute(f"SELECT * FROM {SCHEMA}.{table} ORDER BY {order}").fetchall()
            out[table] = json.loads(json.dumps([dict(r) for r in rows], default=_json_default))
    return out


def _instant(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    return value if value.tzinfo else value.replace(tzinfo=UTC)
