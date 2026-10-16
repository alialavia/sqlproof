"""Numeric keys must be distinct, storable, and shared by every reference."""

from __future__ import annotations

import random
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sqlproof.exceptions import SqlProofGenerationError
from sqlproof.generators.bulk import bulk_table_rows
from sqlproof.generators.graph import dataset_strategy
from sqlproof.generators.rows import table_rows_strategy
from sqlproof.scale.load import generate_dataset
from sqlproof.scenarios import Scenario
from sqlproof.schema.parse_sql import parse_schema_sql


@pytest.mark.parametrize("key", ["PRIMARY KEY", "UNIQUE NOT NULL"])
@settings(max_examples=40, deadline=None)
@given(precision=st.integers(1, 3), scale=st.integers(-3, 4), data=st.data())
def test_numeric_keys_are_distinct_without_storage_rounding(key, precision, scale, data):
    """Oracle: PostgreSQL's digit count and multiples of 10**(-scale)."""
    table = parse_schema_sql(
        f"CREATE TABLE items (n numeric({precision},{scale}) {key})"
    ).table("items")
    count = data.draw(st.integers(1, min(30, 2 * 10**precision - 1)))
    for rows in (
        data.draw(table_rows_strategy(table, count=count)),
        list(bulk_table_rows(table, count=count, rng=random.Random(42), parent_counts={})),
    ):
        values = [row["n"] for row in rows]
        assert len(set(values)) == count
        for value in values:
            coefficient = value * Decimal(10) ** scale
            assert coefficient == coefficient.to_integral_value()
            assert abs(coefficient) < 10**precision


@pytest.mark.parametrize("path", ["hypothesis", "bulk"])
@pytest.mark.parametrize(
    ("sql_type", "check", "expected"),
    [
        ("numeric(1)", "n >= -2 AND n <= 2 AND n NOT IN (-1, 1)", {"-2", "0", "2"}),
        ("numeric(2,-3)", "n > 1001 AND n < 4999 AND n <> 3000", {"2000", "4000"}),
        ("numeric(3,2)", "n IN (0.001, 0.01, 0.02, 0.02)", {"0.01", "0.02"}),
    ],
)
def test_numeric_keys_exhaust_exactly_the_admissible_values(path, sql_type, check, expected):
    """Oracle: manually enumerated finite sets, including holes and off-grid IN values."""
    table = parse_schema_sql(
        f"CREATE TABLE items (n {sql_type} PRIMARY KEY CHECK ({check}))"
    ).table("items")
    count = len(expected)

    def rows(size):
        if path == "bulk":
            return list(bulk_table_rows(table, count=size, rng=random.Random(42), parent_counts={}))
        from hypothesis import find

        return find(table_rows_strategy(table, count=size), lambda _rows: True)

    assert {row["n"] for row in rows(count)} == {Decimal(value) for value in expected}
    with pytest.raises(SqlProofGenerationError, match=f"only {count} distinct non-NULL"):
        rows(count + 1)


def test_nested_numeric_domains_keep_every_inherited_check():
    schema = parse_schema_sql("""
        CREATE DOMAIN base_number AS numeric(2,-3) CHECK (VALUE >= 2000);
        CREATE DOMAIN key_number AS base_number CHECK (VALUE <= 4000);
        CREATE TABLE items (n key_number PRIMARY KEY);
    """)
    rows = list(bulk_table_rows(
        schema.table("items"), count=3, rng=random.Random(42), parent_counts={},
    ))
    assert {row["n"] for row in rows} == {Decimal("2000"), Decimal("3000"), Decimal("4000")}


@settings(max_examples=20, deadline=None)
@given(data=st.data())
def test_numeric_references_use_the_parent_scale_and_checks(data):
    schema = parse_schema_sql("""
        CREATE TABLE parents (id numeric(2,-3) PRIMARY KEY CHECK (id BETWEEN 2000 AND 4000));
        CREATE TABLE children (id int PRIMARY KEY, parent_id numeric REFERENCES parents(id));
        CREATE TABLE links (
            parent_id numeric REFERENCES parents(id), seq numeric(1,2),
            PRIMARY KEY (parent_id, seq)
        );
    """)
    sizes = {"parents": 3, "children": 10, "links": 6}
    bulk = {table.name: list(rows) for table, rows in generate_dataset(schema, sizes, seed=42)}
    for dataset in (data.draw(dataset_strategy(schema, sizes=sizes)), bulk):
        assert {row["id"] for row in dataset["parents"]} == {
            Decimal("2000"), Decimal("3000"), Decimal("4000"),
        }
        for child in ("children", "links"):
            assert {row["parent_id"] for row in dataset[child]} <= {
                Decimal("2000"), Decimal("3000"), Decimal("4000"),
            }


def test_scenarios_resolve_numeric_names_and_per_parent_links():
    schema = parse_schema_sql("""
        CREATE TABLE parents (id numeric(2,-3) PRIMARY KEY CHECK (id >= 2000));
        CREATE TABLE children (id int PRIMARY KEY, parent_id numeric REFERENCES parents(id));
    """)

    def build(s):
        s.row("parents", name="first")
        s.table("parents").fill(2)
        s.table("children").per_parent("parents", count=2)

    compiled = Scenario(build, seed=42, name="numeric").compile(schema)
    dataset = {
        table.name: list(rows) for table, rows in generate_dataset(
            schema, compiled.sizes, seed=compiled.seed, columns=compiled.columns,
        )
    }
    assert compiled.refs["first"] == Decimal("2000")
    assert [row["parent_id"] for row in dataset["children"]] == [
        Decimal("2000"), Decimal("2000"), Decimal("3000"), Decimal("3000"),
        Decimal("4000"), Decimal("4000"),
    ]


@pytest.mark.parametrize("path", ["hypothesis", "bulk"])
def test_numeric_keys_use_the_entire_finite_domain_before_refusing_more_rows(path):
    from hypothesis import find

    table = parse_schema_sql("CREATE TABLE items (n numeric(1,-2) PRIMARY KEY)").table("items")

    def rows(count):
        if path == "bulk":
            return list(bulk_table_rows(table, count=count, rng=random.Random(1), parent_counts={}))
        return find(table_rows_strategy(table, count=count), lambda _rows: True)

    assert {row["n"] for row in rows(19)} == {Decimal(n * 100) for n in range(-9, 10)}
    with pytest.raises(SqlProofGenerationError, match="only 19 distinct non-NULL"):
        rows(20)


def test_numeric_key_checks_that_cannot_be_interpreted_are_reported():
    from sqlproof.exceptions import UnhonoredCheckWarning

    table = parse_schema_sql(
        "CREATE TABLE items (n numeric(2) PRIMARY KEY CHECK (n % 7 = 0))"
    ).table("items")
    with pytest.warns(UnhonoredCheckWarning, match="cannot interpret CHECK"):
        table_rows_strategy(table, count=1)
    with pytest.warns(UnhonoredCheckWarning, match="cannot interpret CHECK"):
        list(bulk_table_rows(table, count=1, rng=random.Random(1), parent_counts={}))


def test_bulk_numeric_references_refuse_to_guess_without_the_parent_definition():
    table = parse_schema_sql(
        "CREATE TABLE child (id int PRIMARY KEY, parent_id numeric REFERENCES parent(id))"
    ).table("child")
    with pytest.raises(SqlProofGenerationError, match="requires parent_tables"):
        list(bulk_table_rows(table, count=1, rng=random.Random(1), parent_counts={"parent": 1}))


@pytest.mark.parametrize("path", ["hypothesis", "bulk"])
def test_a_primary_key_that_references_a_parent_requires_distinct_parent_rows(path):
    from hypothesis import find

    schema = parse_schema_sql("""
        CREATE TABLE parents (id numeric(2,-3) PRIMARY KEY);
        CREATE TABLE child (id numeric PRIMARY KEY REFERENCES parents(id));
    """)
    sizes = {"parents": 1, "child": 2}
    with pytest.raises(SqlProofGenerationError, match="not enough distinct parent keys"):
        if path == "hypothesis":
            find(dataset_strategy(schema, sizes=sizes), lambda _dataset: True)
        else:
            for _, rows in generate_dataset(schema, sizes):
                list(rows)


@pytest.mark.parametrize("key", ["PRIMARY KEY (n)", "PRIMARY KEY (n, m)"])
def test_table_level_primary_keys_make_their_columns_non_nullable(key):
    table = parse_schema_sql(f"CREATE TABLE items (n numeric, m int, {key})").table("items")
    assert not table.column("n").nullable
    assert table.column("m").nullable == (key == "PRIMARY KEY (n)")


def test_an_empty_numeric_element_domain_allows_only_one_unique_empty_array():
    schema = parse_schema_sql("""
        CREATE DOMAIN empty_number AS numeric(1,0) CHECK (VALUE > 0 AND VALUE < 1);
        CREATE TABLE items (n empty_number[] UNIQUE NOT NULL);
    """)
    table = schema.table("items")
    rows = list(bulk_table_rows(table, count=1, rng=random.Random(1), parent_counts={}))
    assert rows == [{"n": []}]
    with pytest.raises(SqlProofGenerationError, match="only the empty numeric array"):
        list(bulk_table_rows(table, count=2, rng=random.Random(1), parent_counts={}))
