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
    """Answers like a backend: ``listed`` maps pg_settings names to
    ``(value, source)``; ``custom`` holds unlisted dotted settings."""

    def __init__(
        self,
        listed: dict[str, tuple[str, str]],
        options: str,
        custom: dict[str, str] | None = None,
    ) -> None:
        self.listed = listed
        self.custom = custom or {}
        self.options = options
        self.set_calls: list[tuple[str, str]] = []

    def query(self, sql: str, params: tuple[str, ...]) -> Any:
        # Like pg.core: the statement runs when query() is called.
        rows: list[tuple[str | None]]
        if sql.startswith("SELECT source FROM pg_settings"):
            matches = [v for k, v in self.listed.items() if k.lower() == params[0].lower()]
            rows = [(matches[0][1],)] if matches else []
        elif sql.startswith("SELECT current_setting"):
            rows = [(self.custom.get(params[0]),)]
        else:
            name, value = params
            self.set_calls.append((name, value))
            rows = [(value,)]

        class Result:
            def getresult(self) -> list[tuple[str | None]]:
                return rows

        return Result()


def test_a_setting_the_pooler_dropped_is_set_for_the_session() -> None:
    native = _FakeNative(
        {"search_path": ('"$user", public', "default")}, "-csearch_path=sqlproof_inv"
    )
    database._reapply_startup_settings(native)
    assert native.set_calls == [("search_path", "sqlproof_inv")]


def test_a_setting_the_server_applied_is_left_alone() -> None:
    native = _FakeNative({"search_path": ("sqlproof_inv", "client")}, "-csearch_path=sqlproof_inv")
    database._reapply_startup_settings(native)
    assert native.set_calls == []


def test_an_applied_setting_the_server_normalized_is_not_set_again() -> None:
    # log_connections=true reads back "on", and it cannot be set after
    # connection start: re-applying it would fail the whole connection.
    native = _FakeNative({"log_connections": ("on", "client")}, "-c log_connections=true")
    database._reapply_startup_settings(native)
    assert native.set_calls == []


def test_setting_names_match_pg_settings_case_insensitively() -> None:
    native = _FakeNative({"DateStyle": ("ISO, MDY", "client")}, "-c datestyle=ISO")
    database._reapply_startup_settings(native)
    assert native.set_calls == []


def test_a_dropped_custom_setting_is_set_and_an_applied_one_is_left_alone() -> None:
    dropped = _FakeNative({}, "-c myapp.flag=1")
    database._reapply_startup_settings(dropped)
    assert dropped.set_calls == [("myapp.flag", "1")]

    applied = _FakeNative({}, "-c myapp.flag=1", custom={"myapp.flag": "1"})
    database._reapply_startup_settings(applied)
    assert applied.set_calls == []


def test_libpqs_resolved_options_take_precedence_over_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("PGOPTIONS", "-c search_path=from_env")
    native = _FakeNative({"search_path": ("public", "default")}, "-c search_path=from_service")
    database._reapply_startup_settings(native)
    assert native.set_calls == [("search_path", "from_service")]
