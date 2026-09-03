from __future__ import annotations

import random
from collections import Counter

from sqlproof.generators.bulk import bulk_table_rows, parent_index_for
from sqlproof.generators.rows import _unique_value
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA = """
CREATE TABLE customers (id bigint PRIMARY KEY, email text NOT NULL);
CREATE TABLE orders (
  id bigint PRIMARY KEY,
  customer_id bigint NOT NULL REFERENCES customers(id),
  qty integer NOT NULL CHECK (qty >= 0),
  note text
);
"""


def test_check_constrained_column_is_in_range_by_construction():
    """Narrowing happens before sampling, so no value is ever rejected
    and retried -- the sampler simply cannot emit a negative qty."""
    schema = parse_schema_sql(SCHEMA)
    rows = list(
        bulk_table_rows(
            schema.table("orders"), count=500,
            rng=random.Random(4), parent_counts={"customers": 5},
        )
    )
    assert all(r["qty"] >= 0 for r in rows)


def test_composite_primary_key_raises_rather_than_generating_bad_data():
    import pytest

    from sqlproof.exceptions import SqlProofGenerationError

    schema = parse_schema_sql(
        "CREATE TABLE m (a bigint, b bigint, PRIMARY KEY (a, b));"
    )
    with pytest.raises(SqlProofGenerationError, match="composite primary key"):
        list(bulk_table_rows(
            schema.table("m"), count=5, rng=random.Random(1), parent_counts={},
        ))


def test_primary_keys_match_the_shared_assignment_function():
    schema = parse_schema_sql(SCHEMA)
    rows = list(
        bulk_table_rows(
            schema.table("customers"), count=5,
            rng=random.Random(1), parent_counts={},
        )
    )
    assert [r["id"] for r in rows] == [
        _unique_value("id", "bigint", i) for i in range(5)
    ]


def test_foreign_keys_only_reference_existing_parent_keys():
    schema = parse_schema_sql(SCHEMA)
    valid = {_unique_value("id", "bigint", i) for i in range(10)}
    rows = list(
        bulk_table_rows(
            schema.table("orders"), count=200,
            rng=random.Random(1), parent_counts={"customers": 10},
        )
    )
    assert len(rows) == 200
    assert all(r["customer_id"] in valid for r in rows)


def test_generation_is_streaming_not_materialised():
    schema = parse_schema_sql(SCHEMA)
    stream = bulk_table_rows(
        schema.table("orders"), count=10_000,
        rng=random.Random(1), parent_counts={"customers": 10},
    )
    assert next(iter(stream)) is not None  # yields before generating all 10k


def test_same_seed_reproduces_identical_rows():
    schema = parse_schema_sql(SCHEMA)
    def gen():
        return list(bulk_table_rows(
            schema.table("orders"), count=50,
            rng=random.Random(99), parent_counts={"customers": 5},
        ))
    assert gen() == gen()


def test_uniform_distribution_spreads_children_across_parents():
    counts = Counter(
        parent_index_for(i, 10, random.Random(i), "uniform", 1.2)
        for i in range(2000)
    )
    assert len(counts) == 10
    assert max(counts.values()) < 400  # no parent dominates


def test_zipf_distribution_concentrates_on_few_parents():
    rng = random.Random(5)
    counts = Counter(
        parent_index_for(i, 100, rng, "zipf", 1.2) for i in range(5000)
    )
    top_share = sum(c for _, c in counts.most_common(5)) / 5000
    assert top_share > 0.30  # heavy tenants exist, unlike uniform
