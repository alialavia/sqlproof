"""Array columns against a real database: introspected as arrays, loaded
by both generators, and carried through counterexample export."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from sqlproof import SqlProof
from sqlproof import _database as database
from sqlproof.cli import main
from sqlproof.config import SqlProofConfig
from sqlproof.exceptions import SqlProofPropertyFailure
from sqlproof.runners.property import sqlproof
from sqlproof.scale.load import load_dataset, truncate
from sqlproof.scenarios.serialize import load_file

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"
SCHEMA = "array_live"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)

SCHEMA_SQL = f"""
CREATE TYPE {SCHEMA}.mood AS ENUM ('calm', 'cross');
CREATE TABLE {SCHEMA}.items (
  id bigint PRIMARY KEY,
  tags text[] NOT NULL,
  counts int[] NOT NULL,
  moods {SCHEMA}.mood[],
  docs jsonb[],
  days date[],
  prices numeric(6, 2)[],
  refs uuid[],
  codes varchar(12)[] UNIQUE
);
"""


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


def test_introspection_sees_arrays(proof: Any) -> None:
    columns = {c.name: c.type for c in proof.schema_info.tables[0].columns}
    assert columns["tags"].kind == "array" and columns["tags"].base.name == "text"
    assert columns["moods"].base.kind == "enum"
    assert columns["codes"].base.modifiers == (12,)


def test_the_bulk_loader_loads_arrays(proof: Any) -> None:
    dsn = os.environ[DSN_ENV]
    with database.connect(dsn, autocommit=True) as conn:
        loaded = load_dataset(conn, proof.schema_info, {"items": 200}, seed=2)
        assert loaded == {"items": 200}
        row = conn.execute(
            f"SELECT count(*) FILTER (WHERE cardinality(tags) > 0),"
            f" max(cardinality(counts)) FROM {SCHEMA}.items"
        ).fetchone()
        truncate(conn, proof.schema_info)
    assert row[0] > 0 and 0 < row[1] <= 5


def test_the_hypothesis_path_inserts_arrays(proof: Any, tmp_path: Path) -> None:
    @sqlproof(proof, sizes={"items": 3}, runs=15, failure_dir=tmp_path)
    def arrays_round_trip(db: Any) -> None:
        rows = db.query(f"SELECT tags, counts FROM {SCHEMA}.items")
        assert all(isinstance(r["tags"], list) for r in rows)

    arrays_round_trip()


def test_a_counterexample_with_arrays_reloads(proof: Any, tmp_path: Path) -> None:
    dsn = os.environ[DSN_ENV]

    @sqlproof(proof, sizes={"items": 2}, runs=5, seed=11, failure_dir=tmp_path)
    def never_holds(db: Any) -> None:
        raise AssertionError("no")

    with pytest.raises(SqlProofPropertyFailure):
        never_holds()
    payload = json.loads((tmp_path / "never_holds.json").read_text(encoding="utf-8"))
    output = tmp_path / "arrays.scenario.yaml"
    assert (
        main(
            [
                "replay",
                str(tmp_path / "never_holds.json"),
                "--as-scenario",
                "--dsn",
                dsn,
                "--schema",
                SCHEMA,
                "--output",
                str(output),
            ]
        )
        == 0
    )
    load_file(output).load(dsn, schema=SCHEMA)
    with database.connect(dsn, row_factory=database.dict_row) as conn:
        loaded = conn.execute(
            f"SELECT tags, counts, moods, docs FROM {SCHEMA}.items ORDER BY id"
        ).fetchall()
    for old, new in zip(payload["dataset"]["items"], loaded, strict=True):
        assert new["tags"] == old["tags"]
        assert new["counts"] == old["counts"]
        assert new["moods"] == old["moods"]
        assert (new["docs"] is None) == (old["docs"] is None)
        if old["docs"] is not None:
            assert [json.loads(d) if isinstance(d, str) else d for d in new["docs"]] == old["docs"]
