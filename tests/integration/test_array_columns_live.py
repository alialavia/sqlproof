"""Array columns against a real database: introspected as arrays, loaded
by both generators, and carried through counterexample export."""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from sqlproof import SqlProof
from sqlproof import _database as database
from sqlproof.cli import main
from sqlproof.config import SqlProofConfig
from sqlproof.exceptions import SqlProofPropertyFailure, UnhonoredCheckWarning
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


# --- Review fixes, one schema per finding, each loaded by both paths ---

FIX_SCHEMA = "array_fix_live"
_POS = f"CREATE DOMAIN {FIX_SCHEMA}.pos AS int CHECK (VALUE > 0) CHECK (VALUE <= 10);"
_TAGS = f"CREATE DOMAIN {FIX_SCHEMA}.tags AS varchar(12)[] CHECK (cardinality(VALUE) <= 2);"


@contextmanager
def _fix_schema(sql: str) -> Iterator[Any]:
    dsn = os.environ[DSN_ENV]
    with database.connect(dsn, autocommit=True) as connection:
        connection.execute(f"DROP SCHEMA IF EXISTS {FIX_SCHEMA} CASCADE")
        try:
            connection.execute(f"CREATE SCHEMA {FIX_SCHEMA}")
            connection.execute(sql)
            yield SqlProof.from_config(SqlProofConfig(connection_string=dsn, schema=FIX_SCHEMA))
        finally:
            connection.execute(f"DROP SCHEMA IF EXISTS {FIX_SCHEMA} CASCADE")


def _bulk_load(proof: Any, sizes: dict[str, int], check_sql: str | None = None) -> Any:
    dsn = os.environ[DSN_ENV]
    with warnings.catch_warnings():
        warnings.simplefilter("error", UnhonoredCheckWarning)
        with database.connect(dsn, autocommit=True) as conn:
            assert load_dataset(conn, proof.schema_info, sizes, seed=7) == sizes
            result = conn.execute(check_sql).fetchone() if check_sql else None
            truncate(conn, proof.schema_info)
    return result


def _hypothesis_load(
    proof: Any, sizes: dict[str, int], tmp_path: Path, check: Callable[[Any], None]
) -> None:
    @sqlproof(proof, sizes=sizes, runs=25, failure_dir=tmp_path)
    def arrays_load(db: Any) -> None:
        check(db)

    with warnings.catch_warnings():
        warnings.simplefilter("error", UnhonoredCheckWarning)
        arrays_load()


def test_element_domain_checks_hold_live(tmp_path: Path) -> None:
    sql = f"""
    {_POS}
    CREATE TABLE {FIX_SCHEMA}.d (id bigint PRIMARY KEY, v {FIX_SCHEMA}.pos[] NOT NULL);
    """

    def check(db: Any) -> None:
        # Cast: PyGreSQL cannot type an array of a domain outside the
        # search_path when it reads one back.
        rows = db.query(f"SELECT v::int[] AS v FROM {FIX_SCHEMA}.d")
        assert all(1 <= x <= 10 for r in rows for x in r["v"])

    with _fix_schema(sql) as proof:
        filled = _bulk_load(
            proof, {"d": 200}, f"SELECT count(*) FROM {FIX_SCHEMA}.d WHERE cardinality(v) > 0"
        )
        assert filled[0] > 0
        _hypothesis_load(proof, {"d": 3}, tmp_path, check)


def test_array_columns_in_composite_and_partial_unique_keys_live(tmp_path: Path) -> None:
    sql = f"""
    CREATE TABLE {FIX_SCHEMA}.owners (id bigint PRIMARY KEY);
    CREATE TABLE {FIX_SCHEMA}.keyed (
      id bigint PRIMARY KEY,
      org int NOT NULL,
      tags text[] NOT NULL,
      docs jsonb[] NOT NULL,
      deleted_at timestamptz,
      UNIQUE (org, tags),
      UNIQUE (org, docs)
    );
    CREATE UNIQUE INDEX keyed_live ON {FIX_SCHEMA}.keyed (tags) WHERE deleted_at IS NULL;
    CREATE TABLE {FIX_SCHEMA}.paired (
      owner_id bigint NOT NULL REFERENCES {FIX_SCHEMA}.owners (id),
      tags text[] NOT NULL,
      PRIMARY KEY (owner_id, tags)
    );
    """
    with _fix_schema(sql) as proof:
        # Not `keyed`: the bulk path assigns keys but draws other columns
        # freely, so it does not keep composite or partial UNIQUEs (of any type).
        _bulk_load(proof, {"owners": 5, "paired": 50})
        _hypothesis_load(proof, {"owners": 2, "keyed": 4, "paired": 4}, tmp_path, lambda db: None)


def test_unique_arrays_fit_their_element_type_live(tmp_path: Path) -> None:
    sql = f"""
    CREATE TYPE {FIX_SCHEMA}.mood AS ENUM ('calm', 'cross');
    {_POS}
    CREATE TABLE {FIX_SCHEMA}.uniques (
      id bigint PRIMARY KEY,
      docs jsonb[] UNIQUE,
      moods {FIX_SCHEMA}.mood[] UNIQUE,
      flags bool[] UNIQUE,
      days date[] UNIQUE,
      seen timestamptz[] UNIQUE,
      ns {FIX_SCHEMA}.pos[] UNIQUE
    );
    """
    with _fix_schema(sql) as proof:
        distinct = _bulk_load(
            proof, {"uniques": 50}, f"SELECT count(DISTINCT moods) FROM {FIX_SCHEMA}.uniques"
        )
        assert distinct[0] == 50
        _hypothesis_load(proof, {"uniques": 4}, tmp_path, lambda db: None)


def test_a_domain_over_an_array_live(tmp_path: Path) -> None:
    sql = f"""
    {_TAGS}
    CREATE TABLE {FIX_SCHEMA}.labelled (
      id bigint PRIMARY KEY,
      labels {FIX_SCHEMA}.tags NOT NULL,
      u {FIX_SCHEMA}.tags UNIQUE
    );
    """
    with _fix_schema(sql) as proof:
        labels = proof.schema_info.table("labelled", FIX_SCHEMA).column("labels").type
        assert labels.kind == "domain" and labels.base.kind == "array"
        assert (labels.base.base.name, labels.base.base.modifiers) == ("varchar", (12,))
        assert labels.check_expressions == ("CHECK (cardinality(VALUE) <= 2)",)
        _bulk_load(proof, {"labelled": 50})
        _hypothesis_load(proof, {"labelled": 3}, tmp_path, lambda db: None)


def test_cardinality_checks_hold_live(tmp_path: Path) -> None:
    sql = f"""
    CREATE TABLE {FIX_SCHEMA}.sized (
      id bigint PRIMARY KEY,
      a int[] NOT NULL CHECK (cardinality(a) >= 2),
      b text[] NOT NULL CHECK (array_length(b, 1) BETWEEN 1 AND 3),
      e int[] NOT NULL CHECK (cardinality(e) = 8)
    );
    """
    with _fix_schema(sql) as proof:
        shortest = _bulk_load(
            proof, {"sized": 100}, f"SELECT min(cardinality(e)) FROM {FIX_SCHEMA}.sized"
        )
        assert shortest[0] == 8
        _hypothesis_load(proof, {"sized": 3}, tmp_path, lambda db: None)
