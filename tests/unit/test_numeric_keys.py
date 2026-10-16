"""Numeric keys must be distinct, storable, and shared by every reference."""

from __future__ import annotations

import random
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import find, given, settings
from hypothesis import strategies as st

from sqlproof.exceptions import SqlProofGenerationError, UnhonoredCheckWarning
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


@settings(max_examples=200, deadline=None)
@given(
    low=st.integers(-400, 400), span=st.integers(0, 400), places=st.integers(-2, 3),
)
def test_numeric_key_units_count_whole_numbers_first_without_losing_any(low, span, places):
    """Oracle: a brute-force listing of `low..high`. The order must be a
    permutation of the domain, list every whole number first, keep the
    remaining order of `_integer_from_one`, and invert through `rank`."""
    from sqlproof.generators.rows import _integer_from_one, _whole_units_first

    high = low + span
    order, rank = _whole_units_first(low, high, places)
    listed = [order(position) for position in range(span + 1)]
    step = 10**places if places > 0 else 1
    assert sorted(listed) == list(range(low, high + 1))
    whole = [unit for unit in listed if unit % step == 0]
    assert listed[: len(whole)] == whole
    previous = _integer_from_one(low, high)
    old = [previous(position) for position in range(span + 1)]
    assert [unit for unit in listed if unit % step] == [unit for unit in old if unit % step]
    assert [rank(unit) for unit in listed] == list(range(span + 1))


@pytest.mark.parametrize("path", ["hypothesis", "bulk"])
@pytest.mark.parametrize("child_type", ["int", "bigint", "numeric(5,0)"])
def test_whole_number_children_can_reference_a_scaled_numeric_key(path, child_type):
    """Contract: PostgreSQL accepts an integer FK into numeric(5,2). Keys
    1.00, 2.00, ... compare equal to 1, 2, ..., so every reference is a
    parent key the child column stores exactly."""
    schema = parse_schema_sql(
        "CREATE TABLE p (id numeric(5,2) PRIMARY KEY);"
        f"CREATE TABLE c (id int PRIMARY KEY, p_id {child_type} NOT NULL REFERENCES p(id));"
    )
    sizes = {"p": 3, "c": 5}
    if path == "bulk":
        generated = generate_dataset(schema, sizes, seed=3)
        dataset = {table.name: list(rows) for table, rows in generated}
    else:
        dataset = find(
            dataset_strategy(schema, sizes=sizes), lambda _dataset: True,
            settings=settings(database=None, deadline=None),
        )
    parents = {row["id"] for row in dataset["p"]}
    assert all(key == key.to_integral_value() for key in parents)
    for row in dataset["c"]:
        assert row["p_id"] in parents
        if child_type != "numeric(5,0)" and path == "bulk":
            assert type(row["p_id"]) is int, "COPY cannot load 1.00 into an integer column"


def test_bulk_references_refuse_a_fractional_key_an_integer_child_cannot_hold():
    """Partial function: with one whole-number key available, the second
    parent key is fractional; copying it into an integer child would round
    it and break the foreign key, so generation stops with a clear error."""
    schema = parse_schema_sql(
        "CREATE TABLE p (id numeric(3,2) PRIMARY KEY CHECK (id > 0 AND id < 1.5));"
        "CREATE TABLE c (id int PRIMARY KEY, p_id int NOT NULL REFERENCES p(id));"
    )
    with pytest.raises(SqlProofGenerationError, match="whole-number keys"):
        for _table, rows in generate_dataset(schema, {"p": 2, "c": 20}, seed=1):
            list(rows)


def test_a_reference_into_another_schema_never_borrows_a_same_named_tables_keys():
    """Contract: `REFERENCES auth.users` names its schema, so the app's own
    `users` table (with a numeric key) must not supply its values."""
    schema = parse_schema_sql(
        "CREATE TABLE users (id numeric(6,2) PRIMARY KEY, name text NOT NULL);"
        "CREATE TABLE notes (id int PRIMARY KEY,"
        " author uuid NOT NULL REFERENCES auth.users(id),"
        " owner numeric(6,2) NOT NULL REFERENCES users(id));"
    )
    dataset = {
        table.name: list(rows)
        for table, rows in generate_dataset(schema, {"users": 2, "notes": 3}, seed=7)
    }
    for row in dataset["notes"]:
        assert isinstance(row["author"], str)
        UUID(row["author"])
        assert row["owner"] in {user["id"] for user in dataset["users"]}


def test_references_through_an_integer_domain_are_converted_too():
    """A child column typed by a domain over an integer stores whole parent
    keys as integers, exactly like a plain integer column."""
    schema = parse_schema_sql(
        "CREATE DOMAIN positive_id AS int CHECK (VALUE > 0);"
        "CREATE TABLE p (id numeric(5,2) PRIMARY KEY);"
        "CREATE TABLE c (id int PRIMARY KEY, p_id positive_id NOT NULL REFERENCES p(id));"
    )
    with pytest.warns(UnhonoredCheckWarning, match="foreign-key column"):
        dataset = {
            table.name: list(rows)
            for table, rows in generate_dataset(schema, {"p": 2, "c": 6}, seed=4)
        }
    parents = {row["id"] for row in dataset["p"]}
    assert all(type(row["p_id"]) is int and row["p_id"] in parents for row in dataset["c"])


def test_bulk_references_refuse_a_key_a_coarser_numeric_child_would_round():
    """Partial function: numeric(5,0) rounds 1.01 to 1, which is not the
    parent key, so a reference that needs the fractional key must raise."""
    schema = parse_schema_sql(
        "CREATE TABLE p (id numeric(3,2) PRIMARY KEY CHECK (id > 0 AND id < 1.5));"
        "CREATE TABLE c (id int PRIMARY KEY, p_id numeric(5,0) NOT NULL REFERENCES p(id));"
    )
    with pytest.raises(SqlProofGenerationError, match="numeric scale 0 would round"):
        for _table, rows in generate_dataset(schema, {"p": 2, "c": 20}, seed=1):
            list(rows)


def test_a_pinned_key_that_defers_takes_its_parents_generated_key():
    """Contract: a scenario-pinned key that is also a foreign key and returns
    GENERATE for a row gets that row's parent key, so each child keeps a
    distinct, existing parent (`profiles.id -> users.id`)."""
    from sqlproof.generators.rows import GENERATE, PinnedKey

    schema = parse_schema_sql(
        "CREATE TABLE p (id numeric(4,2) PRIMARY KEY);"
        "CREATE TABLE c (id numeric(4,2) PRIMARY KEY REFERENCES p(id));"
    )
    parents = [{"id": Decimal(value)} for value in ("1.00", "2.00", "3.00")]
    rows = find(
        table_rows_strategy(
            schema.table("c"), count=3, parent_rows={"p": parents},
            columns={"c.id": PinnedKey(lambda _context: GENERATE)},
        ),
        lambda _rows: True,
        settings=settings(database=None, deadline=None),
    )
    keys = [row["id"] for row in rows]
    assert len(set(keys)) == 3
    assert set(keys) <= {parent["id"] for parent in parents}


def test_domain_checks_stop_at_a_domain_without_a_base_type():
    """A domain model with no base type still contributes its own CHECKs
    instead of looping or failing."""
    from sqlproof.generators.rows import _domain_checks_as_column_checks
    from sqlproof.schema.model import Column, PgType

    domain = PgType("domain", "loose", base=None, check_expressions=("VALUE > 0",))
    checks = _domain_checks_as_column_checks(Column("n", domain, False, None, False))
    assert [check.expression for check in checks] == ["n > 0"]
