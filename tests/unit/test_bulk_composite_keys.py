from __future__ import annotations

import random

from sqlproof.generators.bulk import bulk_table_rows, composite_key_values
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA = """
CREATE TABLE orgs (id bigint PRIMARY KEY);
CREATE TABLE users (id bigint PRIMARY KEY);
CREATE TABLE org_members (
  org_id bigint NOT NULL REFERENCES orgs(id),
  user_id bigint NOT NULL REFERENCES users(id),
  role text NOT NULL,
  PRIMARY KEY (org_id, user_id)
);
"""


def test_composite_keys_are_unique_across_rows():
    schema = parse_schema_sql(SCHEMA)
    table = schema.table("org_members")
    keys = [
        tuple(composite_key_values(table, i).values()) for i in range(1000)
    ]
    assert len(set(keys)) == 1000


def test_composite_key_table_generates_without_raising():
    schema = parse_schema_sql(SCHEMA)
    rows = list(
        bulk_table_rows(
            schema.table("org_members"), count=300,
            rng=random.Random(1),
            parent_counts={"orgs": 20, "users": 20},
        )
    )
    assert len(rows) == 300
    pairs = {(r["org_id"], r["user_id"]) for r in rows}
    assert len(pairs) == 300  # no duplicate composite keys


def test_composite_key_columns_that_are_also_fks_stay_within_parent_range():
    schema = parse_schema_sql(SCHEMA)
    rows = list(
        bulk_table_rows(
            schema.table("org_members"), count=300,
            rng=random.Random(1),
            parent_counts={"orgs": 20, "users": 20},
        )
    )
    assert all(1 <= r["org_id"] <= 20 for r in rows)
    assert all(1 <= r["user_id"] <= 20 for r in rows)


def test_requested_count_exceeding_the_key_space_raises():
    import pytest

    from sqlproof.exceptions import SqlProofGenerationError

    schema = parse_schema_sql(SCHEMA)
    # 3 orgs x 3 users = 9 distinct composite keys; 50 rows is impossible.
    with pytest.raises(SqlProofGenerationError, match="distinct composite keys"):
        list(bulk_table_rows(
            schema.table("org_members"), count=50,
            rng=random.Random(1), parent_counts={"orgs": 3, "users": 3},
        ))
