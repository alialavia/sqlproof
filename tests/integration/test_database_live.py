"""Driver adaptation is checked against PostgreSQL's stored values."""

from __future__ import annotations

import os
from configparser import ConfigParser
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from sqlproof import _database as database
from sqlproof._dsn import conninfo_to_dict, make_conninfo
from sqlproof._range import Range
from sqlproof.client import PostgresSqlProofClient
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


@pytest.mark.parametrize(
    ("options", "expected"),
    [(None, "from_service"), ("-csearch_path=from_dsn", "from_dsn")],
)
def test_startup_options_keep_libpq_service_and_environment_precedence(
    tmp_path, monkeypatch, options, expected
) -> None:
    service = ConfigParser(interpolation=None)
    service["sqlproof_review"] = conninfo_to_dict(os.environ["SQLPROOF_TEST_DATABASE_URL"])
    service["sqlproof_review"]["options"] = "-csearch_path=from_service"
    service_path = tmp_path / "pg_service.conf"
    with service_path.open("w") as file:
        service.write(file, space_around_delimiters=False)
    monkeypatch.setenv("PGSERVICEFILE", str(service_path))
    monkeypatch.setenv("PGOPTIONS", "-csearch_path=from_environment")
    dsn = make_conninfo(service="sqlproof_review", **({"options": options} if options else {}))

    with database.connect(dsn) as conn:
        assert conn.execute("SHOW search_path").fetchone() == (expected,)


def test_a_normalized_backend_only_startup_option_still_connects() -> None:
    # The server reads `true` back as `on`, and ignore_system_indexes can only
    # be set at connection start, so re-applying it after connecting would
    # fail. Any role may set it, so this also runs against hosted databases.
    dsn = make_conninfo(
        os.environ["SQLPROOF_TEST_DATABASE_URL"], options="-c ignore_system_indexes=true"
    )
    with database.connect(dsn) as conn:
        assert conn.execute("SHOW search_path").fetchone() is not None


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


@settings(max_examples=20, deadline=None)
@given(dataset=DATASETS)
def test_copy_can_be_committed_without_an_intervening_query(dataset) -> None:
    """Oracle: a later rollback cannot undo rows whose COPY was committed."""
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute("CREATE TEMPORARY TABLE transport_rows (id integer PRIMARY KEY, value text)")
        conn.commit()
        rows = dataset["transport_rows"]
        copy_table(conn, SCHEMA.tables[0], rows, ["id", "value"])
        assert conn.in_transaction, "COPY must leave the transaction state ready for commit"
        conn.commit()
        assert not conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT id, value FROM transport_rows ORDER BY id").fetchall() == [
            (row["id"], row["value"]) for row in sorted(rows, key=lambda row: row["id"])
        ]


@settings(max_examples=20, deadline=None)
@given(dataset=DATASETS)
def test_copy_can_be_rolled_back_without_an_intervening_query(dataset) -> None:
    """Oracle: the table committed before COPY survives, but the copied rows do not."""
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute("CREATE TEMPORARY TABLE transport_rows (id integer PRIMARY KEY, value text)")
        conn.commit()
        copy_table(conn, SCHEMA.tables[0], dataset["transport_rows"], ["id", "value"])
        conn.rollback()
        assert not conn.in_transaction
        assert conn.execute("SELECT count(*) FROM transport_rows").fetchone() == (0,)


def test_copy_in_autocommit_leaves_no_transaction_open() -> None:
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"], autocommit=True) as conn:
        conn.execute("CREATE TEMPORARY TABLE transport_rows (id integer PRIMARY KEY, value text)")
        copy_table(conn, SCHEMA.tables[0], [{"id": 1, "value": "copied"}], ["id", "value"])
        assert not conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT value FROM transport_rows").fetchone() == ("copied",)


def test_copy_constraint_errors_can_be_rolled_back_without_an_intervening_query() -> None:
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute("CREATE TEMPORARY TABLE transport_rows (id integer PRIMARY KEY, value text)")
        conn.commit()
        with pytest.raises(OSError, match="duplicate key"):
            copy_table(conn, SCHEMA.tables[0], [{"id": 1}, {"id": 1}], ["id", "value"])
        assert conn.in_transaction, "a failed COPY must expose the aborted transaction"
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM transport_rows").fetchone() == (0,)


@pytest.mark.parametrize("error_type", [ValueError, KeyboardInterrupt])
def test_a_copy_stream_error_can_be_rolled_back_and_preserves_the_original_error(
    error_type,
) -> None:
    error = error_type("stream failed")

    class FailingStream:
        def read(self, _size):
            raise error

    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute("CREATE TEMPORARY TABLE transport_rows (id integer PRIMARY KEY, value text)")
        conn.commit()
        with pytest.raises(error_type) as exc, conn.cursor() as cur:
            cur.execute("COPY transport_rows FROM STDIN", stream=FailingStream())
        assert exc.value is error
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute("SELECT count(*) FROM transport_rows").fetchone() == (0,)


@pytest.mark.parametrize("fails", [False, True])
def test_a_connection_block_finishes_its_transaction_immediately_after_copy(fails) -> None:
    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    schema_name = f"sqlproof_copy_{uuid4().hex}"
    schema = parse_schema_sql("CREATE TABLE items (id integer PRIMARY KEY)", schema=schema_name)
    with database.connect(dsn, autocommit=True) as observer:
        observer.execute(f'CREATE SCHEMA "{schema_name}"')
        try:
            observer.execute(f'CREATE TABLE "{schema_name}".items (id integer PRIMARY KEY)')
            outcome = pytest.raises(ValueError, match="caller failed") if fails else nullcontext()
            with outcome, database.connect(dsn) as conn:
                copy_table(conn, schema.tables[0], [{"id": 1}], ["id"])
                if fails:
                    raise ValueError("caller failed")
            assert observer.execute(f'SELECT id FROM "{schema_name}".items').fetchall() == (
                [] if fails else [(1,)]
            )
        finally:
            observer.execute(f'DROP SCHEMA "{schema_name}" CASCADE')


@pytest.mark.parametrize(
    ("sql_type", "minimum"),
    [("smallint", -(2**15)), ("integer", -(2**31)), ("bigint", -(2**63))],
)
def test_the_smallest_integer_parameter_survives_an_explicit_cast(sql_type, minimum) -> None:
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        assert conn.execute(f"SELECT %s::{sql_type}", (minimum,)).fetchone() == (minimum,)


@pytest.mark.parametrize("value", [-1, -0.5, -0.0, Decimal("-1.25"), Decimal("-0.00")])
def test_negative_numeric_parameters_keep_their_sign_inside_casts(value) -> None:
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        # A text parameter cast on the server is independent of numeric adaptation.
        expected = conn.execute("SELECT %s::numeric::text", (str(value),)).fetchone()
        assert conn.execute("SELECT %s::numeric::text", (value,)).fetchone() == expected


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


def test_a_successful_nested_transaction_keeps_its_rows_and_the_outer_transaction() -> None:
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute("CREATE TEMPORARY TABLE transaction_rows (id int PRIMARY KEY)")
        with conn.transaction():
            conn.execute("INSERT INTO transaction_rows VALUES (1)")
        assert conn.in_transaction, "releasing the savepoint must not end the outer transaction"
        assert conn.execute("SELECT id FROM transaction_rows").fetchall() == [(1,)]
        conn.rollback()
        assert not conn.in_transaction


@settings(max_examples=20, deadline=None)
@given(dataset=DATASETS)
def test_a_successful_client_savepoint_keeps_changes_until_the_caller_rolls_back(dataset) -> None:
    """Oracle: stored rows return after the caller rolls back a released savepoint."""
    with database.connect(
        os.environ["SQLPROOF_TEST_DATABASE_URL"], row_factory=database.dict_row
    ) as conn:
        conn.execute("CREATE TEMPORARY TABLE transport_rows (id integer PRIMARY KEY, value text)")
        rows = dataset["transport_rows"]
        copy_table(conn, SCHEMA.tables[0], rows, ["id", "value"])
        conn.commit()
        client = PostgresSqlProofClient(conn)

        with client.savepoint():
            client.execute("DELETE FROM transport_rows")

        assert conn.in_transaction, "releasing a client savepoint must not commit the caller"
        assert client.scalar("SELECT count(*) FROM transport_rows") == 0
        conn.rollback()
        assert client.query("SELECT id, value FROM transport_rows ORDER BY id") == sorted(
            rows, key=lambda row: row["id"]
        )


@pytest.mark.parametrize("error_type", [ValueError, KeyboardInterrupt])
@settings(max_examples=20, deadline=None)
@given(dataset=DATASETS)
def test_a_failed_client_savepoint_restores_rows_and_preserves_the_callers_error(
    error_type, dataset
) -> None:
    """Oracle: pre-savepoint rows survive, including when cancellation interrupts the body."""
    with database.connect(
        os.environ["SQLPROOF_TEST_DATABASE_URL"], row_factory=database.dict_row
    ) as conn:
        conn.execute("CREATE TEMPORARY TABLE transport_rows (id integer PRIMARY KEY, value text)")
        rows = dataset["transport_rows"]
        copy_table(conn, SCHEMA.tables[0], rows, ["id", "value"])
        client = PostgresSqlProofClient(conn)
        error = error_type("the caller's error")

        with pytest.raises(error_type) as exc, client.savepoint():
            client.execute("DELETE FROM transport_rows")
            raise error

        assert exc.value is error, "savepoint cleanup must preserve the original exception"
        assert conn.in_transaction
        assert client.query("SELECT id, value FROM transport_rows ORDER BY id") == sorted(
            rows, key=lambda row: row["id"]
        )


def test_a_sql_error_in_a_client_savepoint_leaves_the_connection_usable() -> None:
    with database.connect(
        os.environ["SQLPROOF_TEST_DATABASE_URL"], row_factory=database.dict_row
    ) as conn:
        client = PostgresSqlProofClient(conn)
        with pytest.raises(database.DatabaseError) as exc, client.savepoint():
            client.execute("SELECT 1 / 0")

        assert database.sqlstate(exc.value) == "22012"
        assert conn.in_transaction
        assert client.scalar("SELECT 42") == 42, "the failed query must not poison the transaction"


@settings(max_examples=20, deadline=None)
@given(dataset=DATASETS)
def test_a_failed_inner_client_savepoint_preserves_changes_in_the_outer_savepoint(dataset) -> None:
    """Oracle: an inner rollback restores the outer update, then the caller can roll it back."""
    with database.connect(
        os.environ["SQLPROOF_TEST_DATABASE_URL"], row_factory=database.dict_row
    ) as conn:
        conn.execute("CREATE TEMPORARY TABLE transport_rows (id integer PRIMARY KEY, value text)")
        rows = dataset["transport_rows"]
        copy_table(conn, SCHEMA.tables[0], rows, ["id", "value"])
        conn.commit()
        client = PostgresSqlProofClient(conn)

        with client.savepoint():
            client.execute("UPDATE transport_rows SET value = 'outer update'")
            with pytest.raises(ValueError, match="inner failure"), client.savepoint():
                client.execute("DELETE FROM transport_rows")
                raise ValueError("inner failure")
            assert client.query("SELECT id, value FROM transport_rows ORDER BY id") == [
                {"id": row["id"], "value": "outer update"}
                for row in sorted(rows, key=lambda row: row["id"])
            ]

        conn.rollback()
        assert client.query("SELECT id, value FROM transport_rows ORDER BY id") == sorted(
            rows, key=lambda row: row["id"]
        )


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
        ("date", date(2024, 2, 29)),
        ("timestamp", datetime(2024, 2, 29, 12, 30, 15, 250000)),
        ("timestamptz", datetime(2024, 2, 29, 12, 30, 15, 250000, tzinfo=UTC)),
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


@pytest.mark.parametrize("style", ["postgres", "postgres_verbose", "sql_standard", "iso_8601"])
@settings(max_examples=20, deadline=None)
@example(value=timedelta(microseconds=100000))
@example(value=timedelta(microseconds=-100000))
@example(value=timedelta(microseconds=120000))
@example(value=timedelta(microseconds=-123000))
@example(value=timedelta(microseconds=123400))
@example(value=timedelta(microseconds=-123450))
@example(value=timedelta(microseconds=123456))
@given(value=st.timedeltas(timedelta(days=-365), timedelta(days=365)))
def test_intervals_preserve_fractional_seconds_in_every_output_style(style, value):
    """Oracle: the original duration, across scalar/array INSERT and COPY."""
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute("SET intervalstyle TO %s", (style,))
        conn.execute("CREATE TEMPORARY TABLE transport_interval (value interval, items interval[])")
        conn.execute("INSERT INTO transport_interval VALUES (%s, %s)", (value, [value, None]))
        table = parse_schema_sql(
            "CREATE TABLE transport_interval (value interval, items interval[])", schema="pg_temp"
        ).tables[0]
        copy_table(conn, table, [{"value": value, "items": [value, None]}], ["value", "items"])
        assert conn.execute("SELECT * FROM transport_interval").fetchall() == [
            (value, [value, None]), (value, [value, None]),
        ]


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


_SUPAVISOR_APPLICATION_NAME = "Supavisor"


@pytest.fixture(scope="module")
def application_name_reaches_the_server() -> None:
    """Gate: these oracles need the server to see the client's
    application_name, which only a direct connection guarantees.

    Supabase's pooler (Supavisor) replaces application_name with its own
    name on the server session, so SHOW can never echo the client's value
    there. The gate only skips when the server reports exactly that
    override; any other mismatch still fails the test.
    """
    probe = "sqlproof-application-name-probe"
    dsn = make_conninfo(os.environ["SQLPROOF_TEST_DATABASE_URL"], application_name=probe)
    with database.connect(dsn) as conn:
        seen = conn.execute("SHOW application_name").fetchone()[0]
    if seen == probe:
        return
    assert seen == _SUPAVISOR_APPLICATION_NAME, (
        f"server reported application_name {seen!r} for {probe!r}, and it is "
        "not the Supavisor pooler override"
    )
    pytest.skip(
        "GATE (pooler): Supavisor rewrites application_name to 'Supavisor'; "
        "these application_name oracles need a direct connection"
    )


@pytest.mark.usefixtures("application_name_reaches_the_server")
def test_connection_options_preserve_backslashes_without_spaces() -> None:
    name = r"sqlproof\review"
    dsn = make_conninfo(os.environ["SQLPROOF_TEST_DATABASE_URL"], application_name=name)
    with database.connect(dsn) as conn:
        assert conn.execute("SHOW application_name").fetchone() == (name,)


@pytest.mark.usefixtures("application_name_reaches_the_server")
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


# --- NUL in text (GROK-09) --------------------------------------------------

NUL_CASES = [
    ("text", "ab\x00cd"),
    ("text[]", ["ok", "ab\x00cd"]),
    ("jsonb", database.Json({"k": "ab\x00cd"})),
]


@pytest.mark.parametrize(("sql_type", "value"), NUL_CASES)
def test_nul_raises_the_same_error_on_insert_and_copy_and_stores_nothing(sql_type, value) -> None:
    """psycopg raised DataError on both paths; the adapter must too, rather than
    letting INSERT store the prefix before the NUL while COPY fails."""
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute(f"CREATE TEMPORARY TABLE nul_rows (value {sql_type})")
        schema = parse_schema_sql(f"CREATE TABLE nul_rows (value {sql_type})", schema="pg_temp")
        with pytest.raises(database.DataError) as insert_error:
            conn.execute("INSERT INTO nul_rows VALUES (%s)", (value,))
        with pytest.raises(database.DataError) as copy_error:
            copy_table(conn, schema.tables[0], [{"value": value}], ["value"])
        assert database.sqlstate(insert_error.value) == database.sqlstate(copy_error.value)
        assert str(insert_error.value) == str(copy_error.value)
        assert conn.execute("SELECT count(*) FROM nul_rows").fetchone() == (0,)
        assert conn.in_transaction, "refusing a value must not disturb the transaction"


# Roughly one character in four is NUL, so most examples hit the refusal path
# and the rest check that ordinary text still round-trips on both paths.
TEXT_WITH_NUL = st.text(
    alphabet=st.one_of(
        st.just("\x00"),
        st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
        st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    ),
    max_size=12,
)


@settings(max_examples=40, deadline=None)
@given(value=TEXT_WITH_NUL)
def test_text_either_round_trips_exactly_or_is_refused_never_truncated(value) -> None:
    """Property: for any text, both write paths store exactly the text or
    raise and store nothing. Oracle: PostgreSQL's stored value."""
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        conn.execute("CREATE TEMPORARY TABLE nul_rows (id int, value text)")
        schema = parse_schema_sql("CREATE TABLE nul_rows (id int, value text)", schema="pg_temp")
        try:
            conn.execute("INSERT INTO nul_rows VALUES (1, %s)", (value,))
            copy_table(conn, schema.tables[0], [{"id": 2, "value": value}], ["id", "value"])
        except database.DataError:
            assert "\x00" in value, "only NUL may be refused"
            assert conn.execute("SELECT count(*) FROM nul_rows").fetchone() == (0,)
        else:
            assert "\x00" not in value, "NUL must never be accepted"
            stored = conn.execute("SELECT value FROM nul_rows ORDER BY id").fetchall()
            assert stored == [(value,), (value,)]


# --- Infinity survives read then write, scalars agree with ranges (GROK-11) --


@pytest.mark.parametrize(
    "literal",
    [
        "daterange('2026-01-01', 'infinity')",
        "daterange('-infinity', '2026-01-01')",
        "tsrange('2026-01-01', 'infinity')",
        "tsrange('-infinity', 'infinity')",
        "tstzrange('2026-01-01 00:00+00', 'infinity')",
        "tstzrange('-infinity', '2026-01-01 00:00+00')",
    ],
)
def test_an_infinite_range_read_and_written_back_is_still_the_same_range(literal) -> None:
    range_type = literal.split("(")[0]
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        (value,) = conn.execute(f"SELECT {literal}").fetchone()
        assert conn.execute(f"SELECT %s::{range_type} = {literal}", (value,)).fetchone() == (True,)
        # upper_inf()/lower_inf() are for unbounded (NULL) ends; an 'infinity'
        # bound is a value, and isfinite() is the predicate that sees it.
        assert conn.execute(
            f"SELECT isfinite(lower(%s::{range_type})) AND isfinite(upper(%s::{range_type}))",
            (value, value),
        ).fetchone() == (False,), "the infinite end must still be infinite after writing back"


@pytest.mark.parametrize("sql_type", ["date", "timestamp", "timestamptz"])
def test_scalar_infinity_equals_the_matching_range_bound(sql_type) -> None:
    """A tstzrange bound and a timestamptz column holding infinity must be the
    same Python value, including timezone-awareness, so they compare and sort."""
    range_type = {"date": "daterange", "timestamp": "tsrange", "timestamptz": "tstzrange"}[sql_type]
    with database.connect(os.environ["SQLPROOF_TEST_DATABASE_URL"]) as conn:
        for sign in ("infinity", "-infinity"):
            rng, scalar = conn.execute(
                f"SELECT {range_type}('{sign}', '{sign}', '[]'), '{sign}'::{sql_type}"
            ).fetchone()
            bound = rng.lower if sign == "-infinity" else rng.upper
            assert bound == scalar
            assert type(bound) is type(scalar)
            if sql_type == "timestamptz":
                assert bound.tzinfo is not None and scalar.tzinfo is not None
            assert sorted([bound, scalar]) == [bound, scalar], "must be orderable"


# --- A failed rollback keeps the caller's exception in reach (GROK-07) -------


def test_a_failed_rollback_raises_with_the_callers_exception_as_direct_context() -> None:
    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    with database.connect(dsn) as conn, database.connect(dsn, autocommit=True) as killer:
        (pid,) = conn.execute("SELECT pg_backend_pid()").fetchone()
        with pytest.raises(database.Error) as exc, conn.transaction():
            killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
            raise ValueError("the caller's original error")
        assert isinstance(exc.value.__context__, ValueError), (
            "the rollback failure must chain the caller's exception directly, "
            "not bury it under a RELEASE error"
        )


def test_client_savepoint_failed_rollback_keeps_the_callers_exception_as_direct_context() -> None:
    from sqlproof.client import PostgresSqlProofClient

    dsn = os.environ["SQLPROOF_TEST_DATABASE_URL"]
    with (
        database.connect(dsn, row_factory=database.dict_row) as conn,
        database.connect(dsn, autocommit=True) as killer,
    ):
        client = PostgresSqlProofClient(conn)
        pid = client.scalar("SELECT pg_backend_pid()")
        with pytest.raises(database.Error) as exc, client.savepoint():
            killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
            raise ValueError("the caller's original error")
        assert isinstance(exc.value.__context__, ValueError)
