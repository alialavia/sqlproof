"""End-to-end tests for `sqlproof.contrib.plpgsql_coverage` against a live Postgres.

Gated on:
  * ``SQLPROOF_TEST_DATABASE_URL`` — a Postgres connection string.
  * ``plpgsql_check`` extension being installed in the target database.

These tests verify the bug fixes that unit tests cover only structurally:

  * #8 — the profiler GUC is enabled, so per-line counters actually
    populate after a function call.
  * #9 — including a ``LANGUAGE sql`` function in ``functions=`` does
    not corrupt the report for ``LANGUAGE plpgsql`` functions.

A fresh per-test schema is created and dropped in ``finally``, so these
tests don't leave residue in the target database.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from sqlproof.client import PsycopgSqlProofClient
from sqlproof.contrib.plpgsql_coverage import (
    assert_nonzero_coverage,
    collect_coverage,
    coverage_session,
    drive_in_order,
    installed_plpgsql_functions,
    plpgsql_check_available,
)

DSN_ENV = "SQLPROOF_TEST_DATABASE_URL"

pytestmark = pytest.mark.skipif(
    DSN_ENV not in os.environ,
    reason=f"set {DSN_ENV} to run Postgres integration tests",
)


@contextmanager
def _temp_schema_with_functions() -> Generator[tuple[PsycopgSqlProofClient, str]]:
    """Create a uniquely-named schema with a plpgsql function and a sql function.

    Skips the test if `plpgsql_check` isn't installed in the target DB —
    we don't try to install it here; that requires superuser-ish
    permissions and is the responsibility of the test environment.

    Yields ``(client, schema_name)``. The schema is dropped on exit even
    if the test raises.
    """
    dsn = os.environ[DSN_ENV]
    schema = f"sqlproof_cov_it_{uuid4().hex[:12]}"

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        client = PsycopgSqlProofClient(conn)

        if not plpgsql_check_available(client):
            pytest.skip(
                "plpgsql_check extension not installed in target database; "
                "run `CREATE EXTENSION plpgsql_check` to enable these tests."
            )

        client.execute(f'CREATE SCHEMA "{schema}"')
        try:
            # `plpgsql_profiler_function_tb('name'::text)` resolves the
            # name through search_path. Functions in our temp schema
            # would otherwise be invisible to the profiler reads, even
            # though their bodies executed and were tracked.
            client.execute(f'SET search_path TO "{schema}", public')
            client.execute(
                f"""
                CREATE FUNCTION "{schema}".plpgsql_fn(p_n integer)
                RETURNS integer
                LANGUAGE plpgsql
                AS $$
                BEGIN
                  IF p_n < 0 THEN
                    RETURN 0;
                  END IF;
                  RETURN p_n + 1;
                END;
                $$;
                """
            )
            client.execute(
                f"""
                CREATE FUNCTION "{schema}".sql_fn(p_n integer)
                RETURNS integer
                LANGUAGE sql
                AS $$ SELECT p_n + 100 $$;
                """
            )
            yield client, schema
        finally:
            client.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_collect_coverage_populates_per_line_data_after_function_call() -> None:
    """Regression for #8: without the profiler GUC, profiler reads return
    rows with NULL `exec_stmts` for every line — looking like 0/0 coverage
    even after the function ran. The fix enables `plpgsql_check.profiler`
    inside `collect_coverage`."""
    with _temp_schema_with_functions() as (db, schema):
        with collect_coverage(db, functions=["plpgsql_fn"], schema=schema) as report:
            db.execute(f'SELECT "{schema}".plpgsql_fn(5)')

        fc = report.functions.get("plpgsql_fn")
        assert fc is not None, "plpgsql_fn should appear in report"
        assert fc.total_executable_lines > 0, (
            "expected per-line data; if this is 0/0 the GUC fix has regressed"
        )
        assert fc.statement_ratio > 0, (
            f"expected nonzero statement coverage after a successful call; "
            f"got {fc.statement_ratio}"
        )


def test_collect_coverage_takes_unhit_branches_into_account() -> None:
    """The `IF p_n < 0 THEN RETURN 0` branch is unreachable from the input 5
    we use; the report should reflect that as <100% statement coverage."""
    with _temp_schema_with_functions() as (db, schema):
        with collect_coverage(db, functions=["plpgsql_fn"], schema=schema) as report:
            db.execute(f'SELECT "{schema}".plpgsql_fn(5)')

        fc = report.functions["plpgsql_fn"]
        # The negative branch's RETURN 0 is never executed, so at least one
        # line should be marked as exec_count == 0.
        unhit = [ln for ln in fc.lines if ln.exec_count == 0]
        assert unhit, "expected at least one unhit line in plpgsql_fn"


def test_collect_coverage_does_not_corrupt_report_when_sql_fn_in_candidates() -> None:
    """Regression for #9: a `LANGUAGE sql` name in `functions=` previously
    caused alphabetically-later plpgsql functions to silently drop from the
    report. The fix filters non-plpgsql names out of the candidate list."""
    with _temp_schema_with_functions() as (db, schema):
        with collect_coverage(
            db, functions=["plpgsql_fn", "sql_fn"], schema=schema
        ) as report:
            db.execute(f'SELECT "{schema}".plpgsql_fn(7)')
            db.execute(f'SELECT "{schema}".sql_fn(7)')

        # plpgsql_fn must still appear with real data.
        assert "plpgsql_fn" in report.functions
        assert report.functions["plpgsql_fn"].statement_ratio > 0
        # sql_fn was filtered — must NOT show as a 0%-covered ghost entry.
        assert "sql_fn" not in report.functions


def test_installed_plpgsql_functions_filters_sql_language() -> None:
    with _temp_schema_with_functions() as (db, schema):
        # Both function names exist; only plpgsql_fn is plpgsql.
        installed = installed_plpgsql_functions(
            db, ["plpgsql_fn", "sql_fn", "ghost_fn"], schema=schema
        )
        assert installed == {"plpgsql_fn"}


def test_coverage_session_end_to_end() -> None:
    """High-level API: `coverage_session` + `drive_in_order` +
    `assert_nonzero_coverage` should produce a clean pass for a function
    that's actually exercised."""
    with _temp_schema_with_functions() as (db, schema):
        drivers = {
            "plpgsql_fn": lambda: db.execute(f'SELECT "{schema}".plpgsql_fn(3)'),
        }
        with coverage_session(
            db,
            ["plpgsql_fn", "sql_fn", "ghost_fn"],
            cluster="testcov",
            schema=schema,
        ) as (report, installed):
            assert installed == {"plpgsql_fn"}, (
                "sql_fn should be filtered (wrong language), "
                "ghost_fn should be filtered (not installed)"
            )
            drive_in_order(installed, drivers, cluster="testcov")

        assert_nonzero_coverage(report, installed, cluster="testcov")
        assert "plpgsql_fn" in report.format()


def test_coverage_session_skips_when_no_candidates_installed_in_schema() -> None:
    """If the user's candidates are all missing or non-plpgsql in the target
    schema, the assertion would pass vacuously. Verify the skip path triggers
    against a real DB."""
    with _temp_schema_with_functions() as (db, schema):
        with pytest.raises(BaseException) as excinfo, coverage_session(
            db,
            ["sql_fn", "ghost_fn"],
            cluster="empty",
            schema=schema,
        ):
            pass
        # pytest.skip raises Skipped (subclass of BaseException, not Exception).
        assert excinfo.value.__class__.__name__ == "Skipped"
        assert "no PL/pgSQL functions" in str(excinfo.value)
