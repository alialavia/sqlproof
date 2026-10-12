"""Synchronous PostgreSQL connections and value adaptation through PyGreSQL."""

from __future__ import annotations

import json
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import inf
from types import TracebackType
from typing import Any, Literal, Self, cast

import pg.core
import pgdb

from sqlproof._range import Range, quote_field

RowFactory = Literal["dict", "tuple"]
dict_row: RowFactory = "dict"
tuple_row: RowFactory = "tuple"
DatabaseError = pgdb.DatabaseError
DataError = pgdb.DataError
Error = pgdb.Error
# PostgreSQL's own SQLSTATE for a NUL byte in text (character_not_in_repertoire),
# which is what COPY reports; raising it client-side keeps both paths alike.
NUL_SQLSTATE = "22021"


@dataclass(frozen=True, slots=True)
class Json:
    """Distinguish a JSON value from an SQL array or SQL NULL."""

    obj: Any


# Both json and jsonb accept serialized JSON text.
Jsonb = Json


def _reject_nul(text: str) -> str:
    """PyGreSQL inlines parameters into the SQL text, where a NUL byte ends
    the C string early and silently truncates the value. Refuse it up front,
    on every write path, with the error PostgreSQL itself gives for COPY."""
    if "\x00" in text:
        error = DataError("PostgreSQL text cannot contain NUL (\\x00); the value was not sent.")
        error.sqlstate = NUL_SQLSTATE
        raise error
    return text


def _json_without_nul(value: Json) -> str:
    pending: list[Any] = [value.obj]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            _reject_nul(current)
        elif isinstance(current, dict):
            mapping = cast(dict[Any, Any], current)  # type: ignore[redundant-cast]
            pending.extend(mapping.keys())
            pending.extend(mapping.values())
        elif isinstance(current, list | tuple):
            pending.extend(cast(Sequence[Any], current))
    return json.dumps(value.obj)


def _adapt(value: Any) -> Any:
    if isinstance(value, str):
        return _reject_nul(value)
    if isinstance(value, Json):
        # PyGreSQL treats strings passed to Json as already serialized.
        return pgdb.Json(_json_without_nul(value))
    if isinstance(value, bytes):
        return pgdb.Binary(value)
    if isinstance(value, Range) or (isinstance(value, Decimal) and not value.is_finite()):
        return str(value)
    if isinstance(value, list):
        if _is_numeric_or_boolean_array(cast(list[Any], value)):  # type: ignore[redundant-cast]
            # PyGreSQL renders these as a typed ARRAY[...] constructor, so
            # the array keeps its type even where PostgreSQL has no column
            # or cast to infer it from (array_length, unnest, SELECT %s).
            return [_adapt(item) for item in cast(Sequence[Any], value)]
        # Other element types (text, UUID, ranges, JSON, dates) are sent as
        # an array literal that takes its type from the column or an
        # explicit cast. pgdb's ARRAY constructor would type them as text[].
        return _text(value)
    if isinstance(value, tuple):
        return tuple(_adapt(item) for item in cast(Sequence[Any], value))
    return value


def _is_numeric_or_boolean_array(value: list[Any]) -> bool:
    """True when every leaf is a finite number, or every leaf is a boolean.

    Only these element types render to an unambiguous PostgreSQL array
    type. Mixed booleans and numbers, all-NULL arrays, empty inner arrays
    and non-finite values fall back to the context-typed literal.
    """
    leaves: list[Any] = []
    pending: list[Any] = [value]
    while pending:
        current = pending.pop()
        if not current:
            return False
        for item in cast(Sequence[Any], current):
            if isinstance(item, list):
                pending.append(item)
            elif item is not None:
                leaves.append(item)
    if not leaves:
        return False
    if all(isinstance(leaf, bool) for leaf in leaves):
        return True
    return all(
        not isinstance(leaf, bool)
        and (
            isinstance(leaf, int)
            or (isinstance(leaf, float | Decimal) and leaf == leaf and leaf not in (inf, -inf))
        )
        for leaf in leaves
    )


def _text(value: Any) -> str:
    if isinstance(value, str):
        return _reject_nul(value)
    if isinstance(value, Json):
        return _json_without_nul(value)
    if isinstance(value, bytes):
        return "\\x" + value.hex()
    if isinstance(value, timedelta):
        return f"{value.days} days {value.seconds} seconds {value.microseconds} microseconds"
    if isinstance(value, list):
        return (
            "{"
            + ",".join(
                "NULL"
                if item is None
                else _text(item)
                if isinstance(item, list)
                else quote_field(_text(item))
                for item in cast(Sequence[Any], value)
            )
            + "}"
        )
    if isinstance(value, tuple):
        return (
            "("
            + ",".join(
                "" if item is None else quote_field(_text(item))
                for item in cast(Sequence[Any], value)
            )
            + ")"
        )
    return str(value)


def copy_text(value: Any) -> str:
    """Encode one value for PostgreSQL's text COPY format."""
    if value is None:
        return r"\N"
    text = _text(value)
    return text.replace("\\", "\\\\").replace("\t", r"\t").replace("\n", r"\n").replace("\r", r"\r")


def sqlstate(error: BaseException) -> str | None:
    """Read the server's SQLSTATE without depending on driver exception names."""
    code = getattr(error, "sqlstate", None)
    return str(code) if code is not None else None


def _range_cast(
    element: Callable[[str], Any],
    negative_infinity: Any = None,
    positive_infinity: Any = None,
) -> Callable[[str], Range]:
    """Build a parser for one range type.

    Date and timestamp bounds may be ``infinity``; those map to the given
    sentinels, mirroring PyGreSQL's handling of scalar infinite dates
    (``date.max`` and ``datetime.max``). Numeric bounds parse ``Infinity``
    natively through ``Decimal`` and integer ranges are never infinite.
    """

    def bound(text: str) -> Any:
        if positive_infinity is not None and text == "infinity":
            return positive_infinity
        if negative_infinity is not None and text == "-infinity":
            return negative_infinity
        return element(text)

    def parse(value: str) -> Range:
        if value == "empty":
            return Range(empty=True)
        # PyGreSQL's stub leaves the record parser's return type unspecified.
        lower, upper = cast(
            tuple[str | None, str | None],
            cast(Any, pg.core).cast_record("(" + value[1:-1] + ")"),
        )
        return Range(
            None if lower is None else bound(lower),
            None if upper is None else bound(upper),
            value[0] + value[-1],
        )

    return parse


class Cursor:
    def __init__(self, cursor: Any, row_factory: RowFactory, connection: Connection) -> None:
        self._cursor = cursor
        self._row_factory: RowFactory = row_factory
        self._connection = connection

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    def execute(self, sql: str, params: Sequence[Any] | None = None, *, stream: Any = None) -> Self:
        self._connection._begin()  # pyright: ignore[reportPrivateUsage]
        self._cursor.execute(sql, tuple(_adapt(value) for value in params or ()))
        if stream is not None:
            # Use the source that started COPY, preserving our already-quoted
            # schema/table identifiers rather than splitting them on dots.
            # This is the only place sqlproof relies on a PyGreSQL private
            # attribute; fail with a clear message if a release removes it.
            putdata = getattr(getattr(self._cursor, "_src", None), "putdata", None)
            if putdata is None:
                msg = (
                    "sqlproof streams COPY data through PyGreSQL's cursor source "
                    "(pgdb.Cursor._src.putdata), which this PyGreSQL version does "
                    "not expose. Pin PyGreSQL to a supported release."
                )
                raise RuntimeError(msg)
            try:
                while chunk := stream.read(8192):
                    putdata(chunk)
            except BaseException as error:
                # PyGreSQL finishes the COPY protocol, then raises OSError
                # for the cancellation. Preserve the stream's original error.
                with suppress(OSError):
                    putdata(error)
                raise
            self._cursor.rowcount = putdata(None)
        return self

    def _row(self, values: Sequence[Any]) -> Any:
        if self._row_factory == "dict":
            names = [column[0] for column in self._cursor.description]
            return dict(zip(names, values, strict=True))
        return tuple(values)

    def fetchone(self) -> Any:
        values = self._cursor.fetchone()
        return None if values is None else self._row(values)

    def fetchall(self) -> list[Any]:
        return [self._row(values) for values in self._cursor.fetchall()]

    def close(self) -> None:
        self._cursor.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class Connection:
    def __init__(
        self,
        connection: Any,
        native: pg.core.Connection,
        row_factory: RowFactory,
        autocommit: bool,
    ) -> None:
        self._connection = connection
        self._row_factory: RowFactory = row_factory
        self._native = native
        self._autocommit = autocommit

    @property
    def in_transaction(self) -> bool:
        return self._native.transaction() in {pg.core.TRANS_INTRANS, pg.core.TRANS_INERROR}

    def _begin(self) -> None:
        if not self._autocommit and self._native.transaction() == pg.core.TRANS_IDLE:
            self._native.source().execute("BEGIN")

    def cursor(self, *, row_factory: RowFactory | None = None) -> Cursor:
        return Cursor(
            self._connection.cursor(),
            self._row_factory if row_factory is None else row_factory,
            self,
        )

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> Cursor:
        cursor = self.cursor()
        try:
            return cursor.execute(sql, params)
        except BaseException:
            cursor.close()
            raise

    def commit(self) -> None:
        if self.in_transaction:
            self._native.source().execute("COMMIT")

    def rollback(self) -> None:
        if self.in_transaction:
            self._native.source().execute("ROLLBACK")

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def transaction(self) -> Generator[None]:
        nested = self.in_transaction
        self._native.source().execute("SAVEPOINT sqlproof_transaction" if nested else "BEGIN")
        try:
            yield
        except BaseException:
            # Undo, then re-raise the caller's exception. If the undo itself
            # fails (connection gone), that error propagates with the caller's
            # exception as its direct __context__, and nothing else is tried.
            if nested:
                self.execute("ROLLBACK TO SAVEPOINT sqlproof_transaction").close()
                self.execute("RELEASE SAVEPOINT sqlproof_transaction").close()
            else:
                self.rollback()
            raise
        else:
            if nested:
                self.execute("RELEASE SAVEPOINT sqlproof_transaction").close()
            else:
                self.commit()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if self.in_transaction:
                if exc_type is None:
                    self.commit()
                else:
                    self.rollback()
        finally:
            self.close()


def connect(
    conninfo: str, *, autocommit: bool = False, row_factory: RowFactory = tuple_row
) -> Connection:
    # Pass the complete DSN directly to libpq. This preserves its URL,
    # SSL, service-file and environment handling without reconstructing it
    # through pgdb.connect's legacy keyword argument escaping.
    native = pg.core.connect(conninfo)
    try:
        connection = pgdb.Connection(native)
        # The adapter owns transaction boundaries using libpq's actual state,
        # including explicit BEGIN and savepoints. Disable pgdb's client flag.
        connection.autocommit = True
        range_casts: dict[str, Callable[[str], Range]] = {
            "int4range": _range_cast(int),
            "int8range": _range_cast(int),
            "numrange": _range_cast(Decimal),
            "daterange": _range_cast(date.fromisoformat, date.min, date.max),
            "tsrange": _range_cast(datetime.fromisoformat, datetime.min, datetime.max),
            "tstzrange": _range_cast(
                datetime.fromisoformat,
                datetime.min.replace(tzinfo=UTC),
                datetime.max.replace(tzinfo=UTC),
            ),
        }
        for name, typecast in range_casts.items():
            cast(Any, connection.type_cache).set_typecast(name, typecast)
        # PyGreSQL returns ordinary timestamptz values timezone-aware but
        # 'infinity' naive; make infinity aware so it equals a tstzrange bound.
        default_timestamptz = cast(Any, connection.type_cache).get_typecast("timestamptz")

        def cast_timestamptz(value: str) -> Any:
            if value == "infinity":
                return datetime.max.replace(tzinfo=UTC)
            if value == "-infinity":
                return datetime.min.replace(tzinfo=UTC)
            return default_timestamptz(value)

        cast(Any, connection.type_cache).set_typecast("timestamptz", cast_timestamptz)
    except BaseException:
        native.close()
        raise
    return Connection(connection, native, row_factory, autocommit)
