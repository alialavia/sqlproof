"""Driver adaptation is checked against PostgreSQL's stored values."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sqlproof import _database as database
from sqlproof._dsn import make_conninfo
from sqlproof._range import Range
from sqlproof.generators.graph import dataset_strategy
from sqlproof.scale.load import copy_table
from sqlproof.schema.parse_sql import parse_schema_sql

pytestmark = pytest.mark.skipif(
    "SQLPROOF_TEST_DATABASE_URL" not in os.environ,
    reason="set SQLPROOF_TEST_DATABASE_URL to run Postgres integration tests",
)

SCHEMA = parse_schema_sql(
    "CREATE TABLE transport_rows (id integer PRIMARY KEY, value text)", schema="pg_temp"
)
TEXT = st.one_of(
    st.none(),
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        max_size=80,
    ),
)
DATASETS = dataset_strategy(
    SCHEMA, sizes={"transport_rows": st.integers(0, 15)}, columns={"transport_rows.value": TEXT}
)


@settings(max_examples=30, deadline=None)
@given(dataset=DATASETS)
def test_copy_preserves_generated_text_and_sql_nulls(dataset) -> None:
    """Oracle: PostgreSQL's stored rows must equal the generated dataset."""
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute("CREATE TEMPORARY TABLE transport_rows (id integer PRIMARY KEY, value text)")
        rows = dataset["transport_rows"]
        assert copy_table(conn, SCHEMA.tables[0], rows, ["id", "value"]) == len(rows)
        stored = conn.cursor(row_factory=database.dict_row)
        with stored:
            stored.execute("SELECT id, value FROM transport_rows ORDER BY id")
            assert stored.fetchall() == sorted(rows, key=lambda row: row["id"])


def test_a_failed_nested_transaction_preserves_the_outer_transaction() -> None:
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute("CREATE TEMPORARY TABLE transaction_rows (id int PRIMARY KEY)")
        conn.execute("INSERT INTO transaction_rows VALUES (1)")
        with pytest.raises(database.DatabaseError) as exc, conn.transaction():
            conn.execute("INSERT INTO transaction_rows VALUES (2)")
            conn.execute("INSERT INTO transaction_rows VALUES (1)")
        assert database.sqlstate(exc.value) == "23505", (
            "duplicate keys must raise a unique violation"
        )
        assert conn.execute("SELECT id FROM transaction_rows").fetchall() == [(1,)]
        assert conn.in_transaction, "the caller's transaction must stay open"


def test_a_generation_error_leaves_copy_and_the_connection_untouched() -> None:
    def failing_rows():
        yield {"id": 1, "value": "first"}
        raise ValueError("generation failed")

    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute("CREATE TEMPORARY TABLE transport_rows (id int PRIMARY KEY, value text)")
        with pytest.raises(ValueError, match="generation failed"):
            copy_table(conn, SCHEMA.tables[0], failing_rows(), ["id", "value"])
        assert conn.execute("SELECT count(*) FROM transport_rows").fetchone() == (0,)


def test_json_null_and_sql_null_remain_distinct() -> None:
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        assert conn.execute(
            "SELECT %s::jsonb IS NULL, %s::jsonb = 'null'::jsonb",
            (None, database.Json(None)),
        ).fetchone() == (True, True)


@pytest.mark.parametrize(
    ("sql_type", "value"),
    [
        ("bytea", b"\x00\xff\t\n\\"),
        ("uuid", UUID("00112233-4455-6677-8899-aabbccddeeff")),
        ("uuid[]", [UUID("00112233-4455-6677-8899-aabbccddeeff"), None]),
        ("numeric", Decimal("-12.345")),
        ("interval", timedelta(days=-2, seconds=5, microseconds=123)),
        ("text[]", [None, "NULL", "", 'quote"\\\t\n', "snowman \u2603"]),
        ("integer[][]", [[1, None], [3, 4]]),
        ("int4range", Range(1, 5)),
        ("int8range", Range(2**40, 2**40 + 5)),
        ("numrange", Range(Decimal("1.25"), Decimal("2.75"), "[]")),
        ("daterange", Range(date(2024, 1, 1), date(2024, 2, 1))),
        ("tsrange", Range(datetime(2024, 1, 1), datetime(2024, 2, 1))),
        (
            "tstzrange",
            Range(
                datetime(2024, 1, 1, tzinfo=UTC),
                datetime(2024, 2, 1, tzinfo=UTC),
            ),
        ),
        ("int4range", Range(empty=True)),
        ("int4range", Range(None, 5, "()")),
        ("int4range[]", [Range(1, 5), Range(empty=True), None]),
    ],
)
def test_parameters_and_copy_each_preserve_postgresql_values(sql_type, value) -> None:
    """Oracle: both stored rows must equal the original Python value."""
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute(f"CREATE TEMPORARY TABLE transport_types (value {sql_type})")
        conn.execute("INSERT INTO transport_types VALUES (%s)", (value,))
        schema = parse_schema_sql(
            f"CREATE TABLE transport_types (value {sql_type})", schema="pg_temp"
        )
        copy_table(conn, schema.tables[0], [{"value": value}], ["value"])
        rows = conn.execute("SELECT value FROM transport_types").fetchall()
        assert rows == [(value,), (value,)]


@pytest.mark.parametrize("value", ["a string", [1, None], {"nested": [True, None]}, None])
def test_json_scalars_arrays_and_objects_preserve_their_json_type(value) -> None:
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        assert conn.execute("SELECT %s::jsonb", (database.Json(value),)).fetchone() == (value,)
        conn.execute("CREATE TEMPORARY TABLE transport_json (value jsonb)")
        schema = parse_schema_sql("CREATE TABLE transport_json (value jsonb)", schema="pg_temp")
        copy_table(conn, schema.tables[0], [{"value": database.Json(value)}], ["value"])
        assert conn.execute("SELECT value, value IS NULL FROM transport_json").fetchone() == (
            value,
            False,
        )


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_special_decimal_parameters_remain_numeric_values(value) -> None:
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        stored = conn.execute("SELECT %s::numeric", (value,)).fetchone()[0]
        assert str(stored) == str(value)


def test_connection_options_preserve_backslashes_without_spaces() -> None:
    name = r"sqlproof\review"
    dsn = make_conninfo(os.environ["SQLPROOF_TEST_DATABASE_URL"], application_name=name)
    with database.connect(dsn) as conn:
        assert conn.execute("SHOW application_name").fetchone() == (name,)


@settings(max_examples=30, deadline=None)
@given(
    name=st.text(
        # PostgreSQL replaces non-printable/non-ASCII application names.
        alphabet=st.characters(min_codepoint=32, max_codepoint=126),
        max_size=30,
    )
)
def test_quoted_connection_options_reach_libpq_unchanged(name) -> None:
    """Oracle: PostgreSQL reports the original option, independently of our parser."""
    name = "sqlproof\\'" + name
    dsn = make_conninfo(os.environ["SQLPROOF_TEST_DATABASE_URL"], application_name=name)
    with database.connect(dsn) as conn:
        assert conn.execute("SHOW application_name").fetchone() == (name,)


def test_copy_uses_the_connections_client_encoding() -> None:
    dsn = make_conninfo(os.environ["SQLPROOF_TEST_DATABASE_URL"], client_encoding="LATIN1")
    with database.connect(dsn) as conn:
        conn.execute("CREATE TEMPORARY TABLE transport_rows (id int PRIMARY KEY, value text)")
        copy_table(conn, SCHEMA.tables[0], [{"id": 1, "value": "caf\u00e9"}], ["id", "value"])
        assert conn.execute("SELECT value FROM transport_rows").fetchone() == ("caf\u00e9",)


def test_a_read_error_during_copy_can_be_rolled_back_without_losing_the_connection() -> None:
    class FailingStream:
        reads = 0

        def read(self, _size):
            self.reads += 1
            if self.reads > 1:
                raise ValueError("read failed")
            return b"1\n"

    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute("CREATE TEMPORARY TABLE transport_rows (id integer)")
        with (
            pytest.raises(ValueError, match="read failed"),
            conn.transaction(),
            conn.cursor() as cur,
        ):
            cur.execute("COPY transport_rows FROM STDIN", stream=FailingStream())
        assert conn.execute("SELECT count(*) FROM transport_rows").fetchone() == (0,)


def test_copy_preserves_quoted_table_and_column_names() -> None:
    ddl = 'CREATE TABLE "transport.rows" ("value.column" text)'
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute(ddl.replace("CREATE TABLE", "CREATE TEMPORARY TABLE"))
        schema = parse_schema_sql(ddl, schema="pg_temp")
        copy_table(conn, schema.tables[0], [{"value.column": 'quote"\\\t\n'}], ["value.column"])
        assert conn.execute('SELECT "value.column" FROM "transport.rows"').fetchone() == (
            'quote"\\\t\n',
        )


def test_parameters_and_copy_preserve_composite_fields() -> None:
    value = (None, 'quote"\\\t\n', Decimal("1.25"))
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute("CREATE TYPE pg_temp.transport_record AS (a int, b text, c numeric)")
        conn.execute("CREATE TEMPORARY TABLE transport_composites (value pg_temp.transport_record)")
        conn.execute("INSERT INTO transport_composites VALUES (%s)", (value,))
        schema = parse_schema_sql(
            "CREATE TYPE transport_record AS (a int, b text, c numeric);"
            "CREATE TABLE transport_composites (value transport_record)",
            schema="pg_temp",
        )
        copy_table(conn, schema.tables[0], [{"value": value}], ["value"])
        assert conn.execute(
            "SELECT (value).a, (value).b, (value).c FROM transport_composites"
        ).fetchall() == [value, value]


def test_autocommit_and_explicit_transactions_follow_the_server_state() -> None:
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute("CREATE TEMPORARY TABLE transaction_rows (id int)")
        assert not conn.in_transaction
        conn.execute("INSERT INTO transaction_rows VALUES (1)")
        conn.execute("BEGIN")
        conn.execute("INSERT INTO transaction_rows VALUES (2)")
        assert conn.in_transaction
        conn.rollback()
        assert not conn.in_transaction
        assert conn.execute("SELECT id FROM transaction_rows").fetchall() == [(1,)]
