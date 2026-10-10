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

if "SQLPROOF_TEST_DATABASE_URL" not in os.environ and os.environ.get("CI"):
    # Locally, no database means skip. In CI, a missing DSN would silently
    # skip every adapter test while the job stays green, so fail collection.
    pytest.fail(
        "SQLPROOF_TEST_DATABASE_URL is unset in CI; the database adapter tests "
        "would all be skipped. Start a PostgreSQL and export the DSN.",
        pytrace=False,
    )

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


def _array_schema(sql_type: str):
    return parse_schema_sql(
        f"CREATE TABLE array_rows (id integer PRIMARY KEY, value {sql_type})", schema="pg_temp"
    )


@st.composite
def rectangular_integer_grids(draw: st.DrawFn) -> list[list[int | None]]:
    # PostgreSQL arrays are rectangular, and an outer array of empty inner
    # arrays ('{{}}') is not a valid literal, so size the grid up front.
    rows = draw(st.integers(0, 3))
    if rows == 0:
        return []
    columns = draw(st.integers(1, 3))
    element = st.one_of(st.none(), st.integers(-(2**31), 2**31 - 1))
    return draw(
        st.lists(
            st.lists(element, min_size=columns, max_size=columns),
            min_size=rows,
            max_size=rows,
        )
    )


# Half of every element's characters come from the set the array literal
# grammar treats specially, so each example exercises the escaping paths
# rather than relying on a rare draw from the full Unicode range.
ARRAY_TEXT = st.one_of(
    st.none(),
    st.just("NULL"),
    st.text(
        alphabet=st.one_of(
            st.sampled_from('\\"{},\t\n\r '),
            st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        ),
        max_size=12,
    ),
)
ARRAY_COLUMNS = {
    "text[]": st.one_of(st.none(), st.lists(ARRAY_TEXT, max_size=5)),
    "integer[][]": st.one_of(st.none(), rectangular_integer_grids()),
}


@pytest.mark.parametrize("sql_type", sorted(ARRAY_COLUMNS))
@settings(max_examples=30, deadline=None)
@given(data=st.data())
def test_generated_arrays_survive_parameters_and_copy_identically(sql_type, data) -> None:
    """Property: for any generated array column, the rows PostgreSQL stores after
    a parameterized INSERT and after COPY both equal the generated rows.

    Oracle: PostgreSQL's stored values, read back through the driver. Catches
    escaping bugs in the array literal encoder for NULL elements, empty
    arrays, quotes, braces, backslashes and whitespace, in either transport.
    """
    schema = _array_schema(sql_type)
    dataset = data.draw(
        dataset_strategy(
            schema,
            sizes={"array_rows": st.integers(0, 8)},
            columns={"array_rows.value": ARRAY_COLUMNS[sql_type]},
        )
    )
    rows = dataset["array_rows"]
    expected = sorted(rows, key=lambda row: row["id"])
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        for table in ("param_rows", "array_rows"):
            conn.execute(
                f"CREATE TEMPORARY TABLE {table} (id integer PRIMARY KEY, value {sql_type})"
            )
        for row in rows:
            conn.execute(
                "INSERT INTO param_rows (id, value) VALUES (%s, %s)", (row["id"], row["value"])
            )
        assert copy_table(conn, schema.tables[0], rows, ["id", "value"]) == len(rows)
        with conn.cursor(row_factory=database.dict_row) as cur:
            cur.execute("SELECT id, value FROM param_rows ORDER BY id")
            assert cur.fetchall() == expected, "parameterized INSERT changed a value"
            cur.execute("SELECT id, value FROM array_rows ORDER BY id")
            assert cur.fetchall() == expected, "COPY changed a value"


def test_an_error_leaving_a_connection_block_discards_its_transaction() -> None:
    """DDL is transactional in PostgreSQL, so a table created inside the block
    exists afterwards only if the adapter committed on exit."""
    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    table = "sqlproof_it_exit_rollback"
    with pytest.raises(RuntimeError, match="abandon"), database.connect(dsn) as conn:
        conn.execute(f"CREATE TABLE {table} (id int)")
        assert conn.in_transaction
        raise RuntimeError("abandon")
    with database.connect(dsn, autocommit=True) as observer:
        assert observer.execute("SELECT to_regclass(%s)", (table,)).fetchone() == (None,), (
            "the table must have been rolled back with the failed block"
        )


def test_a_clean_exit_from_a_connection_block_commits_its_transaction() -> None:
    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    table = "sqlproof_it_exit_commit"
    try:
        with database.connect(dsn) as conn:
            conn.execute(f"CREATE TABLE {table} (id int)")
        with database.connect(dsn, autocommit=True) as observer:
            assert observer.execute("SELECT to_regclass(%s)", (table,)).fetchone() != (None,), (
                "the table must be visible to other sessions after the block"
            )
    finally:
        with database.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(f"DROP TABLE IF EXISTS {table}")


def test_a_top_level_transaction_block_rolls_back_on_error_and_commits_on_success() -> None:
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute("CREATE TEMPORARY TABLE transaction_rows (id int PRIMARY KEY)")
        assert not conn.in_transaction
        with pytest.raises(RuntimeError, match="abandon"), conn.transaction():
            conn.execute("INSERT INTO transaction_rows VALUES (1)")
            assert conn.in_transaction, "the block must open a real transaction"
            raise RuntimeError("abandon")
        assert not conn.in_transaction
        assert conn.execute("SELECT id FROM transaction_rows").fetchall() == []
        with conn.transaction():
            conn.execute("INSERT INTO transaction_rows VALUES (2)")
        assert not conn.in_transaction, "the block must commit, not leave it open"
        assert conn.execute("SELECT id FROM transaction_rows").fetchall() == [(2,)]


# --- Infinite range bounds (GROK-01) ----------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("daterange('2026-01-01', 'infinity')", Range(date(2026, 1, 1), date.max)),
        ("daterange('-infinity', '2026-01-01')", Range(date.min, date(2026, 1, 1))),
        ("tsrange('2026-01-01', 'infinity')", Range(datetime(2026, 1, 1), datetime.max)),
        ("tsrange('-infinity', 'infinity')", Range(datetime.min, datetime.max)),
        (
            "tstzrange('2026-01-01 00:00+00', 'infinity')",
            Range(datetime(2026, 1, 1, tzinfo=UTC), datetime.max.replace(tzinfo=UTC)),
        ),
        (
            "tstzrange('-infinity', '2026-01-01 00:00+00')",
            Range(datetime.min.replace(tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
        ),
        ("numrange(1, 'Infinity')", Range(Decimal(1), Decimal("Infinity"))),
        ("numrange('-Infinity', 1, '(]')", Range(Decimal("-Infinity"), Decimal(1), "(]")),
    ],
)
def test_infinite_range_bounds_read_back_as_sentinels_instead_of_crashing(sql, expected) -> None:
    """Oracle: PyGreSQL's own scalar handling, which returns date.max and
    datetime.max for infinite dates, so ranges agree with the columns next to them."""
    dsn = make_conninfo(os.environ["SQLPROOF_TEST_DATABASE_URL"], options="-c timezone=UTC")
    with database.connect(dsn) as conn:
        assert conn.execute(f"SELECT {sql}").fetchone() == (expected,)


def test_infinite_range_bounds_agree_with_scalar_infinite_dates() -> None:
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        upper, scalar = conn.execute(
            "SELECT upper(daterange('2026-01-01', 'infinity')), 'infinity'::date"
        ).fetchone()
        rng = conn.execute("SELECT daterange('2026-01-01', 'infinity')").fetchone()[0]
        assert rng.upper == upper == scalar


# --- Typed array parameters (GROK-02) ---------------------------------------


@pytest.mark.parametrize(
    ("value", "pg_type"),
    [
        ([1, 2], "integer[]"),
        ([1, None], "integer[]"),
        ([2**40], "bigint[]"),
        ([Decimal("1.5"), 2], "numeric[]"),
        ([True, None], "boolean[]"),
        ([[1, None], [3, 4]], "integer[]"),
    ],
)
def test_numeric_and_boolean_list_parameters_are_typed_without_a_cast(value, pg_type) -> None:
    """Defined behaviour for GROK-02: in a context with no column or cast,
    these lists must still arrive as arrays of the expected type."""
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        assert conn.execute("SELECT %s, pg_typeof(%s)::text", (value, value)).fetchone() == (
            value,
            pg_type,
        )
        assert conn.execute("SELECT array_length(%s, 1)", (value,)).fetchone() == (len(value),)


def test_text_list_parameters_take_their_type_from_the_context() -> None:
    """Defined behaviour for GROK-02: string lists are context-typed literals.
    They work against a typed column or cast and fail, clearly, without one."""
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute("CREATE TYPE pg_temp.mood AS ENUM ('ok', 'sad')")
        conn.execute("CREATE TEMPORARY TABLE moods (value pg_temp.mood[])")
        conn.execute("INSERT INTO moods VALUES (%s)", (["ok", "sad"],))
        assert conn.execute("SELECT value::text[] FROM moods").fetchone() == (["ok", "sad"],)
        assert conn.execute("SELECT 'sad' = ANY(%s)", (["ok", "sad"],)).fetchone() == (True,)
        assert conn.execute("SELECT %s::text[]", (["a"],)).fetchone() == (["a"],)
        with pytest.raises(database.DatabaseError) as exc, conn.transaction():
            conn.execute("SELECT array_length(%s, 1)", (["a"],))
        assert database.sqlstate(exc.value) == "42804", "a datatype mismatch, not a crash"
