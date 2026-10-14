"""The `GENERATE` override sentinel, on both generation paths.

An override that returns `GENERATE` means "generate this value as if the
column had no override". That is what lets a caller pin a column for some
rows and leave the rest to the generator -- how scenario cohorts compile --
so both paths must treat the sentinel exactly like a missing override, at
the same point in their per-column branch order.
"""

from __future__ import annotations

import random

import pytest
from hypothesis import HealthCheck, given, settings

from sqlproof.exceptions import SqlProofSchemaError
from sqlproof.generators import GENERATE
from sqlproof.generators.bulk import bulk_table_rows
from sqlproof.generators.rows import _unique_value, table_rows_strategy
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA = """
CREATE TABLE plans (id bigint PRIMARY KEY);
CREATE TABLE customers (
  id bigint PRIMARY KEY,
  plan_id bigint NOT NULL REFERENCES plans(id),
  tier integer NOT NULL CHECK (tier BETWEEN 1 AND 3),
  handle text NOT NULL UNIQUE,
  status text NOT NULL DEFAULT 'active'
);
"""


def _table():
    return parse_schema_sql(SCHEMA).table("customers")


def _bulk(count: int, columns: dict[str, object], seed: int = 1) -> list[dict[str, object]]:
    return list(
        bulk_table_rows(
            _table(),
            count=count,
            rng=random.Random(seed),
            parent_counts={"plans": 4},
            columns=columns,
        )
    )


def _first_two_tier_one(context: object) -> object:
    return 1 if context.row_index < 2 else GENERATE  # type: ignore[attr-defined]


def test_bulk_generate_pins_some_rows_and_generates_the_rest() -> None:
    rows = _bulk(40, {"customers.tier": _first_two_tier_one})
    assert [r["tier"] for r in rows[:2]] == [1, 1]
    # The rest come from the CHECK-narrowed sampler: in range, and varied.
    rest = [r["tier"] for r in rows[2:]]
    assert set(rest) <= {1, 2, 3}
    assert len(set(rest)) > 1


def test_bulk_generate_is_deterministic_for_a_seed() -> None:
    columns = {"customers.tier": _first_two_tier_one}
    assert _bulk(30, columns, seed=7) == _bulk(30, columns, seed=7)


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        # Falls through to the FK branch: a valid parent key.
        ("plan_id", lambda rows: all(1 <= r["plan_id"] <= 4 for r in rows)),
        # Falls through to the UNIQUE branch: the index-derived value.
        (
            "handle",
            lambda rows: (
                [r["handle"] for r in rows]
                == [_unique_value("handle", "text", i) for i in range(len(rows))]
            ),
        ),
        # Falls through to the DEFAULT branch: omitted, Postgres fills it.
        ("status", lambda rows: all("status" not in r for r in rows)),
    ],
)
def test_bulk_generate_falls_through_to_the_same_branch_as_no_override(
    column: str, expected
) -> None:
    rows = _bulk(10, {f"customers.{column}": lambda _ctx: GENERATE})
    assert expected(rows)


def test_bulk_always_pinned_column_never_builds_a_sampler() -> None:
    """An undimensioned `vector` cannot be sampled; an override that never
    returns GENERATE must keep it working, exactly as before the hook."""
    table = parse_schema_sql(
        "CREATE TABLE docs (id bigint PRIMARY KEY, embedding vector NOT NULL);"
    ).table("docs")
    rows = list(
        bulk_table_rows(
            table,
            count=3,
            rng=random.Random(0),
            parent_counts={},
            columns={"docs.embedding": lambda _ctx: [0.0, 1.0]},
        )
    )
    assert [r["embedding"] for r in rows] == [[0.0, 1.0]] * 3


def test_bulk_generate_on_an_unsampleable_column_raises_clearly() -> None:
    table = parse_schema_sql(
        "CREATE TABLE docs (id bigint PRIMARY KEY, embedding vector NOT NULL);"
    ).table("docs")
    rows = bulk_table_rows(
        table,
        count=1,
        rng=random.Random(0),
        parent_counts={},
        columns={"docs.embedding": lambda _ctx: GENERATE},
    )
    with pytest.raises(SqlProofSchemaError):
        list(rows)


def test_hypothesis_path_generate_pins_some_rows_and_generates_the_rest() -> None:
    strategy = table_rows_strategy(
        _table(),
        count=6,
        parent_rows={"plans": [{"id": 1}, {"id": 2}]},
        columns={"customers.tier": _first_two_tier_one},
    )

    @given(strategy)
    @settings(
        max_examples=20, deadline=None, database=None, suppress_health_check=list(HealthCheck)
    )
    def check(rows: list[dict[str, object]]) -> None:
        assert [r["tier"] for r in rows[:2]] == [1, 1]
        assert all(r["tier"] in {1, 2, 3} for r in rows[2:])
        assert all(r["plan_id"] in {1, 2} for r in rows)
        assert all("status" not in r for r in rows)

    check()


def test_generate_is_a_singleton_with_a_readable_repr() -> None:
    from sqlproof.generators.rows import GENERATE as from_rows

    assert from_rows is GENERATE
    assert repr(GENERATE) == "GENERATE"
