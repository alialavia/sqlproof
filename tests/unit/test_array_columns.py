"""Array columns: parsed as arrays and generated as lists by both paths."""

from __future__ import annotations

import warnings
from datetime import date, datetime
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings

from sqlproof._database import Json, copy_text
from sqlproof.exceptions import UnhonoredCheckWarning
from sqlproof.generators.constraints import unique_rows
from sqlproof.generators.graph import dataset_strategy
from sqlproof.generators.rows import (
    _unique_value,  # pyright: ignore[reportPrivateUsage]
    table_rows_strategy,
)
from sqlproof.generators.typespec import ARRAY_MAX_SIZE, array_size_bounds, spec_for_type
from sqlproof.scale.load import generate_dataset
from sqlproof.schema.checks import analyze_check
from sqlproof.schema.introspect import _load_domains  # pyright: ignore[reportPrivateUsage]
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
    assert (spec.kind, spec.element.kind) == ("array", "text")  # type: ignore[union-attr]
    assert array_size_bounds(spec) == (0, ARRAY_MAX_SIZE)


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


# --- Review fixes: element domains, unique keys, domains over arrays, sizes ---


def _bulk_rows(schema: Any, table: str, count: int, *, seed: int) -> list[dict[str, Any]]:
    [(_, rows)] = list(generate_dataset(schema, {table: count}, seed=seed))
    return list(rows)

DOMAIN_SCHEMA = parse_schema_sql(
    """
    CREATE DOMAIN pos AS int CHECK (VALUE > 0) CHECK (VALUE <= 10);
    CREATE DOMAIN small AS pos CHECK (VALUE < 4);
    CREATE DOMAIN word AS text CHECK (char_length(VALUE) BETWEEN 2 AND 3);
    CREATE TABLE d (
      id bigint PRIMARY KEY,
      v pos[] NOT NULL,
      w pos[],
      s small[] NOT NULL,
      words word[] NOT NULL
    );
    """
)


def _check_domains(row: dict[str, Any]) -> None:
    assert all(1 <= x <= 10 for x in row["v"])
    assert all(1 <= x <= 10 for x in row["w"] or [])
    assert all(1 <= x <= 3 for x in row["s"])
    assert all(2 <= len(x) <= 3 for x in row["words"])


def test_bulk_honours_array_element_domain_checks() -> None:
    rows = _bulk_rows(DOMAIN_SCHEMA, "d", 200, seed=3)
    assert any(row["v"] for row in rows)
    for row in rows:
        _check_domains(row)


@settings(max_examples=40, deadline=None, suppress_health_check=list(HealthCheck))
@given(dataset=dataset_strategy(DOMAIN_SCHEMA, sizes={"d": 3}))
def test_hypothesis_honours_array_element_domain_checks(
    dataset: dict[str, list[dict[str, Any]]],
) -> None:
    for row in dataset["d"]:
        _check_domains(row)


def test_an_unreadable_element_domain_check_warns_on_both_paths() -> None:
    schema = parse_schema_sql(
        """
        CREATE DOMAIN code AS text CHECK (VALUE ~ '^x');
        CREATE TABLE c (id bigint PRIMARY KEY, codes code[] NOT NULL);
        """
    )
    with pytest.warns(UnhonoredCheckWarning, match="codes"):
        _bulk_rows(schema, "c", 2, seed=1)
    with pytest.warns(UnhonoredCheckWarning, match="codes"):
        table_rows_strategy(schema.tables[0], count=2)


def test_an_element_domain_check_does_not_narrow_the_array_itself() -> None:
    schema = parse_schema_sql(
        """
        CREATE DOMAIN tag AS text CHECK (VALUE IN ('a', 'b'));
        CREATE TABLE c (id bigint PRIMARY KEY, tags tag[] NOT NULL);
        """
    )
    rows = _bulk_rows(schema, "c", 50, seed=1)
    assert all(isinstance(r["tags"], list) and set(r["tags"]) <= {"a", "b"} for r in rows)
    assert any(len(r["tags"]) > 1 for r in rows)


KEYS_SCHEMA = parse_schema_sql(
    """
    CREATE TABLE k (
      id bigint PRIMARY KEY,
      org int NOT NULL,
      tags text[] NOT NULL,
      docs jsonb[] NOT NULL,
      deleted_at timestamptz,
      UNIQUE (org, tags),
      UNIQUE (org, docs)
    );
    CREATE UNIQUE INDEX k_live ON k (tags) WHERE deleted_at IS NULL;
    CREATE TABLE kp (
      org int NOT NULL,
      tags text[] NOT NULL,
      PRIMARY KEY (org, tags)
    );
    """
)


@settings(max_examples=30, deadline=None, suppress_health_check=list(HealthCheck))
@given(dataset=dataset_strategy(KEYS_SCHEMA, sizes={"k": 4, "kp": 4}))
def test_array_columns_in_composite_and_partial_unique_keys(
    dataset: dict[str, list[dict[str, Any]]],
) -> None:
    rows = dataset["k"]
    assert unique_rows(rows, ("org", "tags"))
    assert unique_rows(rows, ("org", "docs"))
    live = [r for r in rows if r["deleted_at"] is None]
    assert unique_rows(live, ("tags",))
    assert unique_rows(dataset["kp"], ("org", "tags"))


def test_unique_rows_accepts_arrays_and_json_documents() -> None:
    a = {"tags": ["x"], "docs": [Json({"k": [1]})]}
    b = {"tags": ["x"], "docs": [Json({"k": [1]})]}
    c = {"tags": ["x"], "docs": [Json({"k": [2]})]}
    assert not unique_rows([a, b], ("tags", "docs"))
    assert unique_rows([a, c], ("tags", "docs"))


UNIQUE_SCHEMA = parse_schema_sql(
    """
    CREATE TYPE mood AS ENUM ('calm', 'cross');
    CREATE DOMAIN pos AS int CHECK (VALUE > 0);
    CREATE DOMAIN tags AS text[];
    CREATE TABLE u (
      id bigint PRIMARY KEY,
      docs jsonb[] UNIQUE,
      moods mood[] UNIQUE,
      flags bool[] UNIQUE,
      days date[] UNIQUE,
      seen timestamptz[] UNIQUE,
      ns pos[] UNIQUE,
      labels tags UNIQUE
    );
    """
)
UNIQUE_TABLE = UNIQUE_SCHEMA.tables[0]


def test_unique_array_values_fit_their_element_type() -> None:
    types = {c.name: c.type for c in UNIQUE_TABLE.columns}
    assert _unique_value("docs", types["docs"], 2) == [Json("docs_3")]
    assert _unique_value("ns", types["ns"], 2) == [3]
    assert _unique_value("labels", types["labels"], 2) == ["labels_3"]
    assert _unique_value("days", types["days"], 0) == [date(2000, 1, 1)]
    assert isinstance(_unique_value("seen", types["seen"], 0)[0], datetime)
    for name, allowed in (("moods", {"calm", "cross"}), ("flags", {True, False})):
        values = [_unique_value(name, types[name], i) for i in range(200)]
        assert all(v and set(v) <= allowed for v in values)
        assert len({tuple(v) for v in values}) == 200
        assert max(len(v) for v in values) <= 8  # logarithmic, not linear
    days = [tuple(_unique_value("days", types["days"], i)) for i in range(0, 3_000_000, 999)]
    assert len(set(days)) == len(days)


def _check_unique_columns(rows: list[dict[str, Any]]) -> None:
    for name in ("docs", "moods", "flags", "days", "seen", "ns", "labels"):
        values = [r[name] for r in rows]
        assert unique_rows(rows, (name,)), name
        assert all(isinstance(v, list) and v for v in values), name
    assert all(isinstance(d, Json) for r in rows for d in r["docs"])


def test_bulk_generates_valid_unique_arrays() -> None:
    _check_unique_columns(_bulk_rows(UNIQUE_SCHEMA, "u", 20, seed=1))


@settings(max_examples=10, deadline=None, suppress_health_check=list(HealthCheck))
@given(dataset=dataset_strategy(UNIQUE_SCHEMA, sizes={"u": 5}))
def test_hypothesis_generates_valid_unique_arrays(
    dataset: dict[str, list[dict[str, Any]]],
) -> None:
    _check_unique_columns(dataset["u"])


class _DomainRows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows


class _DomainConnection:
    def execute(self, sql: str, params: tuple[object, ...] = ()) -> _DomainRows:
        del sql, params
        return _DomainRows(
            [
                {
                    "domain_name": "tags",
                    "base_type_name": "_varchar",
                    "base_element_type_name": "varchar",
                    "modifiers": [12],
                    "check_expressions": ["CHECK (cardinality(VALUE) <= 2)"],
                },
                {
                    "domain_name": "moods",
                    "base_type_name": "_mood",
                    "base_element_type_name": "mood",
                    "modifiers": [],
                    "check_expressions": [],
                },
                {
                    "domain_name": "short_tags",
                    "base_type_name": "tags",
                    "base_element_type_name": None,
                    "modifiers": [],
                    "check_expressions": [],
                },
            ]
        )


def test_introspection_resolves_a_domain_over_an_array() -> None:
    mood = parse_schema_sql("CREATE TYPE mood AS ENUM ('calm');").enums[0]
    domains = {d.name: d for d in _load_domains(_DomainConnection(), schema="s", enums=(mood,))}
    tags = domains["tags"]
    assert tags.base is not None and tags.base.kind == "array"
    assert tags.base.base is not None and tags.base.base.name == "varchar"
    assert tags.base.base.modifiers == (12,)
    assert domains["moods"].base.base == mood  # type: ignore[union-attr]
    assert domains["short_tags"].base == tags
    parsed = parse_schema_sql("CREATE DOMAIN tags AS varchar(12)[];").domains[0]
    assert parsed.base == tags.base


def test_cardinality_checks_are_recognised() -> None:
    def atoms(expression: str) -> list[tuple[str, str, Any]]:
        return [(a.kind, a.column, a.payload) for a in analyze_check(expression).atoms]

    assert atoms("cardinality(v) >= 2") == [("cardinality", "v", (">=", 2))]
    assert atoms("CHECK (array_length(v, 1) >= 1 AND array_length(v, 1) <= 3)") == [
        ("cardinality", "v", (">=", 1)),
        ("cardinality", "v", ("<=", 3)),
    ]
    assert atoms("array_length(v, 1) BETWEEN 1 AND 3") == atoms(
        "array_length(v, 1) >= 1 AND array_length(v, 1) <= 3"
    )
    assert atoms("3 = cardinality(v)") == [("cardinality", "v", ("=", 3))]
    assert analyze_check("array_length(v, 2) >= 1").unrecognized


SIZED_SCHEMA = parse_schema_sql(
    """
    CREATE DOMAIN pair AS int[] CHECK (cardinality(VALUE) = 2);
    CREATE TABLE z (
      id bigint PRIMARY KEY,
      a int[] NOT NULL CHECK (cardinality(a) >= 2),
      b text[] NOT NULL CHECK (array_length(b, 1) BETWEEN 1 AND 3),
      c int[] CHECK (cardinality(c) < 2),
      e int[] NOT NULL CHECK (cardinality(e) = 8),
      p pair NOT NULL
    );
    """
)


def _check_sizes(row: dict[str, Any]) -> None:
    assert 2 <= len(row["a"]) <= ARRAY_MAX_SIZE
    assert 1 <= len(row["b"]) <= 3
    assert row["c"] is None or len(row["c"]) < 2
    assert len(row["e"]) == 8
    assert len(row["p"]) == 2


def test_bulk_honours_cardinality_checks() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", UnhonoredCheckWarning)
        rows = _bulk_rows(SIZED_SCHEMA, "z", 100, seed=5)
    for row in rows:
        _check_sizes(row)


@settings(max_examples=40, deadline=None, suppress_health_check=list(HealthCheck))
@given(dataset=dataset_strategy(SIZED_SCHEMA, sizes={"z": 3}))
def test_hypothesis_honours_cardinality_checks(
    dataset: dict[str, list[dict[str, Any]]],
) -> None:
    for row in dataset["z"]:
        _check_sizes(row)


def test_unique_integer_arrays_stay_inside_the_element_domain() -> None:
    v = DOMAIN_SCHEMA.tables[0].column("v").type  # pos: 1..10
    values = [_unique_value("v", v, i) for i in range(100)]
    assert values[:3] == [[1], [2], [3]]
    assert all(1 <= x <= 10 for value in values for x in value)
    assert len({tuple(value) for value in values}) == 100
