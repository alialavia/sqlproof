"""Driver-free checks on the database adapter's own logic."""

from __future__ import annotations

import io
from datetime import UTC, date, datetime
from decimal import Decimal

import pgdb
import pytest
from hypothesis import given
from hypothesis import strategies as st

from sqlproof import _database as database
from sqlproof._range import Range
from sqlproof.schema._postgres import node_message, parse_sql

VALID_BOUNDS = ("[)", "[]", "()", "(]")


def test_a_failed_handshake_closes_the_native_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """If pgdb cannot wrap a freshly opened libpq connection, the socket must
    not leak: the native handle is closed and the original error propagates."""

    class FakeNative:
        closed = False

        def close(self) -> None:
            self.closed = True

    native = FakeNative()
    monkeypatch.setattr(database.pg.core, "connect", lambda conninfo: native)

    def failing_wrapper(_native: object) -> None:
        raise RuntimeError("wrapper failed")

    monkeypatch.setattr(database.pgdb, "Connection", failing_wrapper)
    with pytest.raises(RuntimeError, match="wrapper failed"):
        database.connect("dbname=ignored")
    assert native.closed, "the libpq connection must be closed when setup fails"


@pytest.mark.parametrize("bounds", VALID_BOUNDS)
def test_every_postgresql_bound_form_is_accepted(bounds: str) -> None:
    rng = Range(1, 2, bounds)
    assert str(rng) == f'{bounds[0]}"1","2"{bounds[1]}'
    assert not rng.isempty


@given(bounds=st.text(max_size=3).filter(lambda text: text not in VALID_BOUNDS))
def test_any_other_bound_string_is_rejected(bounds: str) -> None:
    """Property: Range accepts exactly PostgreSQL's four bound spellings.
    Oracle: the PostgreSQL range literal grammar, independent of the class."""
    with pytest.raises(ValueError, match="Range bounds"):
        Range(1, 2, bounds)


def test_an_empty_range_reports_empty_and_renders_as_postgresql_does() -> None:
    rng = Range(empty=True)
    assert rng.isempty
    assert str(rng) == "empty"


# --- Parameter adaptation (GROK-02) ---------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        [1, 2],
        [1, None],
        [Decimal("1.5"), 2],
        [1.5, 2.0],
        [True, None],
        [[1, None], [3, 4]],
        [2**40],
    ],
)
def test_numeric_and_boolean_lists_are_sent_as_typed_arrays(value: list[object]) -> None:
    """These must stay Python lists so PyGreSQL renders an ARRAY[...] constructor,
    which keeps the array typed where PostgreSQL has no context to infer it."""
    assert database._adapt(value) == value  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    ("value", "literal"),
    [
        (["a", "b"], '{"a","b"}'),
        ([], "{}"),
        ([None, None], "{NULL,NULL}"),
        ([1, True], '{"1","True"}'),
        ([Decimal("Infinity")], '{"Infinity"}'),
        ([float("nan")], '{"nan"}'),
        ([[1, 2], []], '{{"1","2"},{}}'),
        ([Range(1, 5)], '{"[\\"1\\",\\"5\\")"}'),
    ],
)
def test_other_lists_become_context_typed_array_literals(value: list[object], literal: str) -> None:
    """Strings, mixed, all-NULL, empty, non-finite and range elements have no
    unambiguous array type, so they are sent as literals typed by the column
    or an explicit cast."""
    assert database._adapt(value) == literal  # pyright: ignore[reportPrivateUsage]


# --- Private third-party attributes (GROK-04) ------------------------------


def test_copy_fails_clearly_when_pygresql_hides_its_cursor_source() -> None:
    class CursorWithoutSource:
        rowcount = -1

        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            pass

    class ConnectionStub:
        def _begin(self) -> None:
            pass

    cursor = database.Cursor(CursorWithoutSource(), database.tuple_row, ConnectionStub())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match=r"pgdb\.Cursor\._src\.putdata"):
        cursor.execute("COPY t FROM STDIN", stream=io.BytesIO(b"1\n"))


def test_installed_pygresql_still_exposes_the_cursor_source() -> None:
    """Fails loudly on a PyGreSQL upgrade that removes the attribute COPY uses."""
    assert "_src" in pgdb.Cursor.__init__.__code__.co_names or hasattr(pgdb.Cursor, "_src"), (
        "pgdb.Cursor no longer sets _src; update the COPY path in sqlproof._database"
    )


def test_installed_postgast_still_exposes_the_protobuf_message() -> None:
    """Fails loudly on a postgast upgrade that removes the attribute rendering uses."""
    (statement,) = parse_sql("SELECT 1")
    message = node_message(statement)
    assert message.DESCRIPTOR.name == "RawStmt"


def test_rendering_fails_clearly_when_postgast_hides_the_protobuf_message() -> None:
    class NodeWithoutMessage:
        pass

    with pytest.raises(RuntimeError, match=r"AstNode\._pb"):
        node_message(NodeWithoutMessage())  # type: ignore[arg-type]


# --- Public surface (GROK-08) ----------------------------------------------


def test_the_public_database_module_exposes_the_adapter_surface_examples_use() -> None:
    from sqlproof import database as public

    assert public.connect is database.connect
    assert public.DatabaseError is database.DatabaseError
    assert public.sqlstate is database.sqlstate
    assert public.Range is Range
    assert set(public.__all__) >= {"connect", "Connection", "DatabaseError", "Json", "sqlstate"}


# --- NUL in text (GROK-09) --------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "a\x00b",
        ["ok", "a\x00b"],
        [["nested", "a\x00b"]],
        ("field", "a\x00b"),
        database.Json("a\x00b"),
        database.Json({"k": ["a\x00b"]}),
        database.Json({"a\x00b": 1}),
    ],
)
def test_nul_is_refused_on_both_write_paths_before_anything_is_sent(value: object) -> None:
    """PyGreSQL inlines parameters, so a NUL would silently truncate the value
    instead of failing. Both the parameter and COPY encoders must refuse it
    with PostgreSQL's own SQLSTATE for the condition."""
    for encode in (database._adapt, database.copy_text):  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(database.DataError, match="NUL") as exc:
            encode(value)
        assert database.sqlstate(exc.value) == database.NUL_SQLSTATE


def test_text_without_nul_still_passes_through_unchanged() -> None:
    assert database._adapt("plain \\x00 text") == "plain \\x00 text"  # pyright: ignore[reportPrivateUsage]
    assert database.copy_text("tab\there") == "tab\\there"


# --- Infinite bounds write back as infinity (GROK-11) -----------------------


@pytest.mark.parametrize(
    ("rng", "literal"),
    [
        (Range(date(2026, 1, 1), date.max), '["2026-01-01",infinity)'),
        (Range(date.min, date(2026, 1, 1)), '[-infinity,"2026-01-01")'),
        (Range(datetime(2026, 1, 1), datetime.max), '["2026-01-01 00:00:00",infinity)'),
        (
            Range(datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC)),
            "[-infinity,infinity)",
        ),
        (Range(date(2026, 1, 1), date(9999, 12, 30)), '["2026-01-01","9999-12-30")'),
    ],
)
def test_sentinel_bounds_render_as_infinity_so_ranges_round_trip(rng: Range, literal: str) -> None:
    assert str(rng) == literal
