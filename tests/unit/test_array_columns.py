"""Array columns: parsed as arrays and generated as lists by both paths."""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings

from sqlproof._database import Json, copy_text
from sqlproof.generators.graph import dataset_strategy
from sqlproof.generators.rows import _unique_value  # pyright: ignore[reportPrivateUsage]
from sqlproof.generators.typespec import ARRAY_MAX_SIZE, spec_for_type
from sqlproof.scale.load import generate_dataset
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA = parse_schema_sql(
    """
    CREATE TYPE mood AS ENUM ('calm', 'cross');
    CREATE TABLE t (
      id bigint PRIMARY KEY,
      tags text[] NOT NULL,
      grid int[][],
      moods mood[],
      docs jsonb[],
      codes varchar(12)[] UNIQUE
    );
    """
)
TABLE = SCHEMA.tables[0]


def test_array_types_are_parsed_with_their_element() -> None:
    types = {c.name: c.type for c in TABLE.columns}
    assert (types["tags"].kind, types["tags"].name, types["tags"].base.name) == (  # type: ignore[union-attr]
        "array",
        "text[]",
        "text",
    )
    assert types["grid"].array_dim == 2
    assert types["moods"].base.kind == "enum"  # type: ignore[union-attr]
    spec = spec_for_type(types["tags"])
    assert (spec.kind, spec.element.kind, spec.max_size) == ("array", "text", ARRAY_MAX_SIZE)  # type: ignore[union-attr]


def _check(row: dict[str, Any]) -> None:
    assert isinstance(row["tags"], list) and all(isinstance(v, str) for v in row["tags"])
    assert len(row["tags"]) <= ARRAY_MAX_SIZE
    for value in row["grid"] or []:
        assert isinstance(value, int)
    for value in row["moods"] or []:
        assert value in ("calm", "cross")
    for value in row["docs"] or []:
        assert isinstance(value, Json)
    assert row["codes"] == [f"codes_{row['id']}"]  # unique: one element per row


def test_the_bulk_path_generates_lists() -> None:
    [(_, rows)] = list(generate_dataset(SCHEMA, {"t": 30}, seed=4))
    for row in rows:
        _check(row)


@settings(max_examples=30, deadline=None, suppress_health_check=list(HealthCheck))
@given(dataset=dataset_strategy(SCHEMA, sizes={"t": 3}))
def test_the_hypothesis_path_generates_lists(dataset: dict[str, list[dict[str, Any]]]) -> None:
    for row in dataset["t"]:
        _check(row)


def test_arrays_render_as_postgres_array_literals() -> None:
    assert copy_text(["a", 'b "q"']) == '{"a","b \\\\"q\\\\""}'
    assert copy_text([Json({"k": 1})]) == '{"{\\\\"k\\\\": 1}"}'
    assert _unique_value("codes", "varchar[]", 2) == ["codes_3"]
    assert _unique_value("ids", "int4[]", 2) == [3]
