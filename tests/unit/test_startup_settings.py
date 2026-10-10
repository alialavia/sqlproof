"""Startup ``options`` must reach the session even through a pooler.

Supavisor and PgBouncer drop the startup packet's ``options``, so a
``-csearch_path=...`` DSN connected fine and then silently ran against
the default search_path (relation "orders" does not exist). ``connect``
now reads each setting back and re-applies the ones that were lost.
"""

from __future__ import annotations

from typing import Any

import pytest

from sqlproof import _database as database


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ("-csearch_path=app", [("search_path", "app")]),
        ("-c search_path=app", [("search_path", "app")]),
        ("--search-path=app", [("search_path", "app")]),
        (
            "-c search_path=a,b -cstatement_timeout=5s",
            [("search_path", "a,b"), ("statement_timeout", "5s")],
        ),
        (r"-capplication_name=a\ b", [("application_name", "a b")]),
        (r"-csearch_path=a\\b", [("search_path", r"a\b")]),
        ("-B 100 -c", []),
        ("", []),
    ],
)
def test_startup_settings_are_split_like_the_server_splits_them(options, expected) -> None:
    assert database.startup_settings(options) == expected


class _FakeNative:
    """Answers like a pooled backend that never saw the startup options."""

    def __init__(self, settings: dict[str, str]) -> None:
        self.settings = settings
        self.set_calls: list[tuple[str, str]] = []

    def query(self, sql: str, params: tuple[str, ...]) -> Any:
        # Like pg.core: the statement runs when query() is called.
        if sql.startswith("SELECT current_setting"):
            row: tuple[str | None] = (self.settings.get(params[0]),)
        else:
            name, value = params
            self.set_calls.append((name, value))
            self.settings[name] = value
            row = (value,)

        class Result:
            def getresult(self) -> list[tuple[str | None]]:
                return [row]

        return Result()


def test_a_setting_the_pooler_dropped_is_set_for_the_session() -> None:
    native = _FakeNative({"search_path": '"$user", public'})
    database._reapply_startup_settings(
        native, "host=pooler dbname=postgres options='-csearch_path=sqlproof_inv'"
    )
    assert native.set_calls == [("search_path", "sqlproof_inv")]


def test_a_setting_the_server_applied_is_left_alone() -> None:
    native = _FakeNative({"search_path": "sqlproof_inv"})
    database._reapply_startup_settings(
        native, "postgresql://h/postgres?options=-csearch_path%3Dsqlproof_inv"
    )
    assert native.set_calls == []


def test_pgoptions_is_the_fallback_like_libpq(monkeypatch) -> None:
    monkeypatch.setenv("PGOPTIONS", "-c search_path=from_env")
    native = _FakeNative({"search_path": "public"})
    database._reapply_startup_settings(native, "host=pooler dbname=postgres")
    assert native.set_calls == [("search_path", "from_env")]
