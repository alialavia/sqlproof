"""Driver-free checks on the database adapter's own logic."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from sqlproof import _database as database
from sqlproof._range import Range

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
