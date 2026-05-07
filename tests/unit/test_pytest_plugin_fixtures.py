"""Unit tests for the SqlProof pytest plugin's fixtures and DSN resolution.

End-to-end behavior of `proof` / `db` / `supabase_proof` against a live
database is covered in ``tests/integration/test_pytest_plugin_fixtures.py``.
This file uses pytester to verify the fixtures' wiring without a real DB:
DSN resolution order, the skip path, and the override pattern.
"""

from __future__ import annotations

import pytest


def test_sqlproof_database_url_resolves_cli_flag(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        def test_uses_cli_flag(sqlproof_database_url):
            assert sqlproof_database_url == "postgresql://flag/db"
        """
    )
    result = pytester.runpytest_subprocess(
        "--sqlproof-database-url=postgresql://flag/db", "-v"
    )
    result.assert_outcomes(passed=1)


def test_sqlproof_database_url_resolves_sqlproof_env(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLPROOF_DATABASE_URL", "postgresql://env-primary/db")
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    pytester.makepyfile(
        """
        def test_uses_env(sqlproof_database_url):
            assert sqlproof_database_url == "postgresql://env-primary/db"
        """
    )
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1)


def test_sqlproof_database_url_falls_back_to_supabase_db_url(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SQLPROOF_DATABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://supabase-fallback/db")
    pytester.makepyfile(
        """
        def test_uses_supabase_db_url(sqlproof_database_url):
            assert sqlproof_database_url == "postgresql://supabase-fallback/db"
        """
    )
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1)


def test_sqlproof_database_url_prefers_cli_over_env(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLPROOF_DATABASE_URL", "postgresql://env/should-not-win")
    pytester.makepyfile(
        """
        def test_cli_wins(sqlproof_database_url):
            assert sqlproof_database_url == "postgresql://cli/wins"
        """
    )
    result = pytester.runpytest_subprocess(
        "--sqlproof-database-url=postgresql://cli/wins", "-v"
    )
    result.assert_outcomes(passed=1)


def test_sqlproof_database_url_prefers_sqlproof_env_over_supabase(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLPROOF_DATABASE_URL", "postgresql://primary/wins")
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://fallback/should-not-win")
    pytester.makepyfile(
        """
        def test_primary_env_wins(sqlproof_database_url):
            assert sqlproof_database_url == "postgresql://primary/wins"
        """
    )
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1)


def test_sqlproof_database_url_skips_when_none_configured(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SQLPROOF_DATABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    pytester.makepyfile(
        """
        def test_should_skip(sqlproof_database_url):
            assert False, "fixture should have skipped"
        """
    )
    result = pytester.runpytest("-v", "-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*no database URL configured*"])


def test_proof_fixture_can_be_overridden_in_user_conftest(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a user defines their own `proof` fixture (e.g. to register
    external tables), it shadows the plugin's default. The chain still
    flows: their override can request `sqlproof_database_url` from the
    plugin, and `db` (which depends on `proof`) picks up their version."""
    monkeypatch.setenv("SQLPROOF_DATABASE_URL", "postgresql://test/db")

    pytester.makeconftest(
        """
        import pytest

        class MockProof:
            def client_for_dataset(self, dataset):
                # tiny sentinel context manager
                from contextlib import contextmanager

                @contextmanager
                def cm():
                    yield "client-from-overridden-proof"

                return cm()

            def disconnect(self):
                pass

        @pytest.fixture(scope="session")
        def proof(sqlproof_database_url):
            assert sqlproof_database_url == "postgresql://test/db"
            yield MockProof()
        """
    )
    pytester.makepyfile(
        """
        def test_db_uses_overridden_proof(db):
            assert db == "client-from-overridden-proof"
        """
    )
    result = pytester.runpytest("-v")
    result.assert_outcomes(passed=1)
