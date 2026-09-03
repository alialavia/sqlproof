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

# A composite PK with exactly one FK-backed column and one plain
# counter column -- the shape that exposed the declaration-order bug
# (Fix round 1). Declared both ways so the fix can be checked for
# order-independence: `org_events` has the FK column declared first,
# `org_events_seq_first` has it declared last.
MIXED_SCHEMA = """
CREATE TABLE orgs (id bigint PRIMARY KEY);
CREATE TABLE org_events (
  org_id bigint NOT NULL REFERENCES orgs(id),
  seq bigint NOT NULL,
  PRIMARY KEY (org_id, seq)
);
CREATE TABLE org_events_seq_first (
  org_id bigint NOT NULL REFERENCES orgs(id),
  seq bigint NOT NULL,
  PRIMARY KEY (seq, org_id)
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


def test_fk_column_reaches_all_parents_when_declared_first():
    """PRIMARY KEY (org_id, seq) -- the FK column declared first."""
    schema = parse_schema_sql(MIXED_SCHEMA)
    rows = list(
        bulk_table_rows(
            schema.table("org_events"), count=1000,
            rng=random.Random(1), parent_counts={"orgs": 50},
        )
    )
    assert len(rows) == 1000
    assert {r["org_id"] for r in rows} == set(range(1, 51))
    assert len({(r["org_id"], r["seq"]) for r in rows}) == 1000


def test_fk_column_reaches_all_parents_when_declared_last():
    """PRIMARY KEY (seq, org_id) -- the FK column declared last.

    Before Fix round 1, the last-declared column absorbed the entire
    row index and every earlier column was pinned to a single value.
    Declaring org_id last used to pin it to parent #1 for the whole
    table; the fix must make this order-independent.
    """
    schema = parse_schema_sql(MIXED_SCHEMA)
    rows = list(
        bulk_table_rows(
            schema.table("org_events_seq_first"), count=1000,
            rng=random.Random(1), parent_counts={"orgs": 50},
        )
    )
    assert len(rows) == 1000
    assert {r["org_id"] for r in rows} == set(range(1, 51))
    assert len({(r["org_id"], r["seq"]) for r in rows}) == 1000


def test_two_non_foreign_key_composite_columns_raises():
    import pytest

    from sqlproof.exceptions import SqlProofGenerationError

    schema = parse_schema_sql(
        "CREATE TABLE m (a bigint NOT NULL, b bigint NOT NULL, PRIMARY KEY (a, b));"
    )
    table = schema.table("m")
    with pytest.raises(SqlProofGenerationError, match="not foreign keys"):
        composite_key_values(table, 0)
    # bulk_table_rows must fail the same way instead of silently
    # misassigning -- the space check can't catch this case (neither
    # column has a radix to bound), so it must surface once rows are
    # actually requested.
    with pytest.raises(SqlProofGenerationError, match="not foreign keys"):
        list(bulk_table_rows(
            table, count=5, rng=random.Random(1), parent_counts={},
        ))
