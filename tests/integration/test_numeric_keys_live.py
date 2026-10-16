"""PostgreSQL constraints and stored keys are independent generation oracles."""

from __future__ import annotations

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sqlproof import SqlProof, postgres
from sqlproof.config import SqlProofConfig
from sqlproof.scale.load import generate_dataset, load_dataset
from sqlproof.scenarios import Scenario
from sqlproof.schema.parse_sql import parse_schema_sql

pytestmark = pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)

SCHEMA_SQL = """
CREATE DOMAIN base_number AS numeric(2,-3) CHECK (VALUE >= 2000);
CREATE DOMAIN key_number AS base_number CHECK (VALUE <= 4000);
CREATE TABLE parents (
    id key_number PRIMARY KEY, code numeric(1,2) UNIQUE NOT NULL CHECK (code > 0)
);
CREATE TABLE children (id int PRIMARY KEY, parent_id numeric NOT NULL REFERENCES parents(id));
CREATE TABLE profiles (id numeric PRIMARY KEY REFERENCES parents(id));
CREATE TABLE leaves (id int PRIMARY KEY, profile_id numeric NOT NULL REFERENCES profiles(id));
CREATE TABLE links (
    parent_id numeric NOT NULL REFERENCES parents(id), seq numeric(1,2),
    PRIMARY KEY (parent_id, seq)
);
CREATE TABLE arrays (id int PRIMARY KEY, codes numeric(1,-2)[] UNIQUE NOT NULL);
"""

SIZES = {"parents": 3, "children": 6, "profiles": 3, "leaves": 6, "links": 6, "arrays": 20}


@pytest.fixture
def key_schema():
    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    name = f"sqlproof_numeric_keys_{uuid4().hex}"
    with postgres.connect(dsn, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
        try:
            conn.execute(f'SET search_path TO "{name}"')
            conn.execute(SCHEMA_SQL)
            yield dsn, name, conn
        finally:
            conn.execute(f'DROP SCHEMA "{name}" CASCADE')


@pytest.mark.parametrize("source", ["sql", "database"])
@pytest.mark.parametrize("path", ["hypothesis", "bulk", "scenario"])
def test_numeric_keys_and_references_survive_loading_unchanged(key_schema, source, path):
    dsn, name, conn = key_schema
    proof = SqlProof.from_config(SqlProofConfig(connection_string=dsn, schema=name))
    if source == "sql":
        proof.schema_info = parse_schema_sql(SCHEMA_SQL, schema=name)

    def assert_stored(query, dataset):
        for table, rows in dataset.items():
            stored = query(f'SELECT * FROM "{name}"."{table}"')
            def key(row, table_name=table):
                return (row["parent_id"], row["seq"]) if table_name == "links" else row["id"]

            assert sorted(stored, key=key) == sorted(rows, key=key), table
        assert {row["id"] for row in dataset["parents"]} == {
            Decimal("2000"), Decimal("3000"), Decimal("4000"),
        }

    if path == "hypothesis":
        @settings(max_examples=20, deadline=None, database=None)
        @given(data=st.data())
        def check(data):
            dataset = data.draw(proof.dataset_strategy(sizes=SIZES))
            with proof.client_for_dataset(dataset) as db:
                assert_stored(db.query, dataset)

        check()
        return

    columns = None
    sizes = SIZES
    if path == "scenario":
        def build(s):
            s.row("parents", name="first")
            s.table("parents").fill(2)
            s.table("children").per_parent("parents", count=2)
            s.row("profiles", name="profile")
            s.table("profiles").fill(2)
            s.table("leaves").per_parent("profiles", count=2)
            s.table("links").fill(6)
            s.table("arrays").fill(20)

        compiled = Scenario(build, seed=42, name="numeric").compile(proof.schema_info)
        assert compiled.refs["first"] == compiled.refs["profile"] == Decimal("2000")
        columns, sizes = compiled.columns, compiled.sizes
    dataset = {
        table.name: list(rows) for table, rows in generate_dataset(
            proof.schema_info, sizes, seed=42, columns=columns,
        )
    }
    load_dataset(conn, proof.schema_info, sizes, seed=42, columns=columns)
    with conn.cursor(row_factory=postgres.dict_row) as cur:
        assert_stored(lambda sql: cur.execute(sql).fetchall(), dataset)
