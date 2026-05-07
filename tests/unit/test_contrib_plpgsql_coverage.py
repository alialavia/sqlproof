"""Unit tests for `sqlproof.contrib.plpgsql_coverage`.

End-to-end behavior of the profiler against a live database is exercised
in ``tests/integration/test_plpgsql_coverage.py``; this file focuses on
the pure-Python helpers and on the SQL query shape via a stub client.
"""

from __future__ import annotations

import io
import re
from contextlib import redirect_stdout
from typing import Any

import pytest
from _pytest.outcomes import Skipped

from sqlproof.contrib.plpgsql_coverage import (
    FunctionCoverage,
    LineCoverage,
    PlpgsqlCheckNotAvailable,
    PlpgsqlCoverageReport,
    assert_nonzero_coverage,
    collect_coverage,
    coverage_session,
    drive_in_order,
    installed_plpgsql_functions,
    plpgsql_check_available,
)


class StubClient:
    """SqlProofClient stand-in. Routes queries to canned responses
    matched by substring; records every SQL execution for later assert.

    Tests configure responses via ``add_response(substring, rows)`` and
    inspect ``calls`` to verify the expected SQL was emitted.
    """

    def __init__(
        self,
        *,
        extension_installed: bool = True,
        plpgsql_functions: set[str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._responses: list[tuple[re.Pattern[str], list[dict[str, Any]]]] = []
        self._failures: list[tuple[re.Pattern[str], Exception]] = []
        if extension_installed:
            self.add_response(
                r"FROM pg_extension WHERE extname = 'plpgsql_check'",
                [{"?column?": 1}],
            )
        else:
            self.add_response(
                r"FROM pg_extension WHERE extname = 'plpgsql_check'",
                [],
            )
        if plpgsql_functions is not None:
            self._plpgsql_functions = plpgsql_functions
        else:
            self._plpgsql_functions = set()

    def add_response(self, pattern: str, rows: list[dict[str, Any]]) -> None:
        self._responses.append((re.compile(pattern), rows))

    def fail_on(self, pattern: str, exc: Exception) -> None:
        """Make `query` raise `exc` when the SQL matches `pattern`. Used to
        exercise the contrib's defensive `except Exception` branches."""
        self._failures.append((re.compile(pattern), exc))

    def execute(self, sql: str, *params: Any) -> int:
        self.calls.append((sql, params))
        return 0

    def query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        for pattern, exc in self._failures:
            if pattern.search(sql):
                raise exc
        # pg_proc + plpgsql language match: respect the candidates filter
        # (when present) so we exercise the same code path real callers do.
        if "FROM pg_proc" in sql and "lanname = 'plpgsql'" in sql:
            if params and isinstance(params[-1], list):
                # candidates path: intersect
                candidates = set(params[-1])
                hits = candidates & self._plpgsql_functions
            else:
                hits = self._plpgsql_functions
            return [{"name": name} for name in sorted(hits)]
        for pattern, rows in self._responses:
            if pattern.search(sql):
                return rows
        return []

    def scalar(self, sql: str, *params: Any) -> Any:
        rows = self.query(sql, *params)
        if not rows:
            return None
        first_row = rows[0]
        return next(iter(first_row.values()))


# ---------------------------------------------------------------------------
# plpgsql_check_available
# ---------------------------------------------------------------------------


def test_plpgsql_check_available_returns_true_when_extension_present() -> None:
    db = StubClient(extension_installed=True)
    assert plpgsql_check_available(db) is True


def test_plpgsql_check_available_returns_false_when_extension_absent() -> None:
    db = StubClient(extension_installed=False)
    assert plpgsql_check_available(db) is False


# ---------------------------------------------------------------------------
# installed_plpgsql_functions
# ---------------------------------------------------------------------------


def test_installed_plpgsql_functions_returns_all_when_no_candidates() -> None:
    db = StubClient(plpgsql_functions={"f_one", "f_two", "f_three"})
    assert installed_plpgsql_functions(db) == {"f_one", "f_two", "f_three"}


def test_installed_plpgsql_functions_intersects_with_candidates() -> None:
    db = StubClient(plpgsql_functions={"f_one", "f_two"})
    # Only f_one is installed-and-plpgsql; f_three is missing entirely;
    # f_sql_only is missing because it's filtered by language at the
    # SQL level (StubClient's plpgsql_functions set excludes it).
    assert installed_plpgsql_functions(
        db, ["f_one", "f_three", "f_sql_only"]
    ) == {"f_one"}


def test_installed_plpgsql_functions_returns_empty_set_for_empty_candidates() -> None:
    db = StubClient(plpgsql_functions={"f_one"})
    # Empty candidates is a fast-path: should not even hit the database.
    before = len(db.calls)
    assert installed_plpgsql_functions(db, []) == set()
    assert len(db.calls) == before, "empty candidates path should skip the DB hit"


def test_installed_plpgsql_functions_accepts_iterables() -> None:
    db = StubClient(plpgsql_functions={"a", "b"})
    assert installed_plpgsql_functions(db, iter(["a", "missing"])) == {"a"}


# ---------------------------------------------------------------------------
# drive_in_order
# ---------------------------------------------------------------------------


def test_drive_in_order_calls_drivers_in_sorted_order() -> None:
    invoked: list[str] = []
    drivers = {
        "c_third": lambda: invoked.append("c_third"),
        "a_first": lambda: invoked.append("a_first"),
        "b_second": lambda: invoked.append("b_second"),
    }
    drive_in_order({"c_third", "a_first", "b_second"}, drivers, cluster="x")
    assert invoked == ["a_first", "b_second", "c_third"]


def test_drive_in_order_raises_keyerror_with_cluster_hint_for_missing_driver() -> None:
    drivers: dict[str, Any] = {"foo": lambda: None}
    with pytest.raises(KeyError) as excinfo:
        drive_in_order({"foo", "bar"}, drivers, cluster="brand")
    msg = str(excinfo.value)
    assert "brand-coverage" in msg
    assert "'bar'" in msg


def test_drive_in_order_logs_failing_driver_then_reraises() -> None:
    def boom() -> None:
        raise RuntimeError("driver exploded")

    drivers = {"working": lambda: None, "broken": boom}
    out = io.StringIO()
    with redirect_stdout(out), pytest.raises(RuntimeError, match="driver exploded"):
        drive_in_order({"working", "broken"}, drivers, cluster="orgs")

    log = out.getvalue()
    assert "[orgs-coverage]" in log
    assert "broken" in log
    assert "RuntimeError" in log


def test_drive_in_order_does_not_log_for_successful_drivers() -> None:
    drivers = {"a": lambda: None, "b": lambda: None}
    out = io.StringIO()
    with redirect_stdout(out):
        drive_in_order({"a", "b"}, drivers, cluster="x")
    assert out.getvalue() == ""


# ---------------------------------------------------------------------------
# assert_nonzero_coverage
# ---------------------------------------------------------------------------


def _report_with(coverages: dict[str, float]) -> PlpgsqlCoverageReport:
    return PlpgsqlCoverageReport(
        functions={
            name: FunctionCoverage(name=name, statement_ratio=ratio)
            for name, ratio in coverages.items()
        }
    )


def test_assert_nonzero_coverage_passes_when_all_above_zero() -> None:
    report = _report_with({"a": 0.5, "b": 0.99, "c": 1.0})
    assert_nonzero_coverage(report, {"a", "b", "c"}, cluster="x")  # no raise


def test_assert_nonzero_coverage_fails_listing_uncovered_functions() -> None:
    report = _report_with({"a": 0.5, "b": 0.0, "c": 0.0})
    with pytest.raises(AssertionError) as excinfo:
        assert_nonzero_coverage(report, {"a", "b", "c"}, cluster="brand")
    msg = str(excinfo.value)
    assert "brand" in msg
    assert "['b', 'c']" in msg


def test_assert_nonzero_coverage_with_threshold_treats_at_threshold_as_below() -> None:
    report = _report_with({"a": 0.79, "b": 0.80, "c": 0.81})
    # threshold=0.80 → b is *at* threshold and counts as "below".
    with pytest.raises(AssertionError, match=r"80%.*\['a', 'b'\]"):
        assert_nonzero_coverage(report, {"a", "b", "c"}, cluster="x", threshold=0.80)


def test_assert_nonzero_coverage_with_empty_installed_passes_vacuously() -> None:
    report = PlpgsqlCoverageReport()
    assert_nonzero_coverage(report, set(), cluster="empty")  # no raise


def test_assert_nonzero_coverage_treats_missing_function_as_zero() -> None:
    report = _report_with({"a": 0.5})
    with pytest.raises(AssertionError, match=r"\['ghost'\]"):
        assert_nonzero_coverage(report, {"a", "ghost"}, cluster="x")


# ---------------------------------------------------------------------------
# coverage_session — error / skip paths (the happy path is integration-tested)
# ---------------------------------------------------------------------------


def test_coverage_session_skips_when_extension_missing_by_default() -> None:
    db = StubClient(extension_installed=False)
    with (
        pytest.raises(Skipped, match="plpgsql_check extension not installed"),
        coverage_session(db, ["my_func"], cluster="x"),
    ):
        pass


def test_coverage_session_raises_when_extension_missing_and_skip_disabled() -> None:
    db = StubClient(extension_installed=False)
    with pytest.raises(PlpgsqlCheckNotAvailable), coverage_session(
        db, ["my_func"], cluster="x", skip_on_missing_extension=False
    ):
        pass


def test_coverage_session_skips_when_no_candidates_are_installed() -> None:
    db = StubClient(extension_installed=True, plpgsql_functions=set())
    with (
        pytest.raises(Skipped, match="no PL/pgSQL functions to profile"),
        coverage_session(db, ["ghost_a", "ghost_b"], cluster="x"),
    ):
        pass


def test_coverage_session_yields_empty_when_no_installed_and_skip_disabled() -> None:
    db = StubClient(extension_installed=True, plpgsql_functions=set())
    with coverage_session(
        db,
        ["ghost"],
        cluster="x",
        skip_on_no_installed=False,
    ) as (report, installed):
        assert installed == set()
        assert report.functions == {}


def test_coverage_session_logs_drift_for_missing_candidates() -> None:
    db = StubClient(extension_installed=True, plpgsql_functions={"installed_fn"})
    db.add_response(
        r"plpgsql_profiler_function_tb",
        [{"lineno": 1, "exec_stmts": 1, "source": "SELECT 1"}],
    )
    db.add_response(r"plpgsql_coverage_statements", [{"ratio": 1.0}])
    db.add_response(r"plpgsql_coverage_branches", [{"ratio": 1.0}])

    out = io.StringIO()
    with redirect_stdout(out), coverage_session(
        db,
        ["installed_fn", "ghost_a", "ghost_b"],
        cluster="brand",
    ) as (_report, installed):
        assert installed == {"installed_fn"}

    log = out.getvalue()
    assert "[brand-coverage]" in log
    assert "schema/DB drift" in log
    assert "ghost_a" in log
    assert "ghost_b" in log
    assert "installed_fn" not in log  # only missing names should be listed


def test_coverage_session_does_not_log_drift_when_all_candidates_installed() -> None:
    db = StubClient(extension_installed=True, plpgsql_functions={"a", "b"})
    db.add_response(
        r"plpgsql_profiler_function_tb",
        [{"lineno": 1, "exec_stmts": 1, "source": "SELECT 1"}],
    )
    db.add_response(r"plpgsql_coverage_statements", [{"ratio": 1.0}])
    db.add_response(r"plpgsql_coverage_branches", [{"ratio": 1.0}])

    out = io.StringIO()
    with redirect_stdout(out), coverage_session(db, ["a", "b"], cluster="x") as _:
        pass

    assert "drift" not in out.getvalue()


# ---------------------------------------------------------------------------
# collect_coverage — error path + GUC + filter integration via stubs
# ---------------------------------------------------------------------------


def test_collect_coverage_raises_when_extension_missing() -> None:
    db = StubClient(extension_installed=False)
    with (
        pytest.raises(PlpgsqlCheckNotAvailable, match="plpgsql_check extension"),
        collect_coverage(db),
    ):
        pass


def test_collect_coverage_enables_profiler_guc_before_reset() -> None:
    """Regression for #8 — without the SET, profiler reads return empty rows."""
    db = StubClient(extension_installed=True)
    with collect_coverage(db):
        pass

    sql_calls = [c[0] for c in db.calls]
    set_idx = next(
        i for i, sql in enumerate(sql_calls) if "plpgsql_check.profiler = on" in sql
    )
    reset_idx = next(
        i for i, sql in enumerate(sql_calls) if "plpgsql_profiler_reset_all" in sql
    )
    assert set_idx < reset_idx, (
        "GUC must be enabled BEFORE the profiler reset; otherwise the "
        "reset runs against an off profiler and counters never get set "
        "on subsequent calls."
    )


def test_collect_coverage_filters_explicit_functions_to_plpgsql_only() -> None:
    """Regression for #9 — passing a non-plpgsql name in functions= used to
    corrupt the report for valid plpgsql names."""
    db = StubClient(extension_installed=True, plpgsql_functions={"plpgsql_fn"})
    db.add_response(
        r"plpgsql_profiler_function_tb",
        [{"lineno": 1, "exec_stmts": 2, "source": "SELECT 1"}],
    )
    db.add_response(r"plpgsql_coverage_statements", [{"ratio": 1.0}])
    db.add_response(r"plpgsql_coverage_branches", [{"ratio": 1.0}])

    with collect_coverage(db, functions=["plpgsql_fn", "sql_fn"]) as report:
        pass

    assert set(report.functions.keys()) == {"plpgsql_fn"}


# ---------------------------------------------------------------------------
# PlpgsqlCoverageReport.format
# ---------------------------------------------------------------------------


def test_report_branch_coverage_returns_recorded_ratio() -> None:
    report = PlpgsqlCoverageReport(
        functions={
            "fn_a": FunctionCoverage(
                name="fn_a", statement_ratio=0.8, branch_ratio=0.6
            ),
        }
    )
    assert report.branch_coverage("fn_a") == 0.6


def test_report_branch_coverage_returns_zero_for_unknown_function() -> None:
    report = PlpgsqlCoverageReport()
    assert report.branch_coverage("ghost") == 0.0


# ---------------------------------------------------------------------------
# _collect_function_coverage — defensive paths when profiler queries fail
# ---------------------------------------------------------------------------


def test_collect_coverage_skips_function_when_profiler_function_tb_errors() -> None:
    """`plpgsql_profiler_function_tb` can fail for functions the profiler
    has no data on (e.g. function dropped between reset and read). The
    contrib catches and drops the function rather than corrupting the
    rest of the report."""
    db = StubClient(extension_installed=True, plpgsql_functions={"flaky_fn"})
    db.fail_on(r"plpgsql_profiler_function_tb", RuntimeError("profiler tb exploded"))

    with collect_coverage(db, functions=["flaky_fn"]) as report:
        pass

    # Function dropped from the report — no ghost zero-coverage entry.
    assert "flaky_fn" not in report.functions


def test_collect_coverage_falls_back_to_zero_when_statements_query_errors() -> None:
    """`plpgsql_coverage_statements` can fail (e.g. extension version
    mismatch on the helper); the function should still appear in the
    report with stmt_ratio=0 rather than crashing the whole collection."""
    db = StubClient(extension_installed=True, plpgsql_functions={"fn_a"})
    db.add_response(
        r"plpgsql_profiler_function_tb",
        [{"lineno": 1, "exec_stmts": 1, "source": "BEGIN"}],
    )
    db.fail_on(
        r"plpgsql_coverage_statements", RuntimeError("statements helper exploded")
    )
    db.add_response(r"plpgsql_coverage_branches", [{"ratio": 0.5}])

    with collect_coverage(db, functions=["fn_a"]) as report:
        pass

    fc = report.functions["fn_a"]
    assert fc.statement_ratio == 0.0
    assert fc.branch_ratio == 0.5  # branch query still ran


def test_collect_coverage_falls_back_to_zero_when_branches_query_errors() -> None:
    """Symmetric defense for the branches helper."""
    db = StubClient(extension_installed=True, plpgsql_functions={"fn_a"})
    db.add_response(
        r"plpgsql_profiler_function_tb",
        [{"lineno": 1, "exec_stmts": 1, "source": "BEGIN"}],
    )
    db.add_response(r"plpgsql_coverage_statements", [{"ratio": 0.7}])
    db.fail_on(
        r"plpgsql_coverage_branches", RuntimeError("branches helper exploded")
    )

    with collect_coverage(db, functions=["fn_a"]) as report:
        pass

    fc = report.functions["fn_a"]
    assert fc.statement_ratio == 0.7
    assert fc.branch_ratio == 0.0


def test_report_format_returns_placeholder_when_empty() -> None:
    report = PlpgsqlCoverageReport()
    assert report.format() == "No PL/pgSQL functions tracked."


def test_report_format_renders_per_function_summary() -> None:
    report = PlpgsqlCoverageReport(
        functions={
            "fn_a": FunctionCoverage(
                name="fn_a",
                statement_ratio=0.75,
                branch_ratio=0.5,
                lines=[
                    LineCoverage(lineno=1, source="BEGIN", exec_count=1),
                    LineCoverage(lineno=2, source="RAISE NOTICE 'x'", exec_count=0),
                ],
            ),
        }
    )
    output = report.format()
    assert "1/1 functions fully covered" not in output  # ratio is 0.75
    assert "0/1 functions fully covered" in output
    assert "fn_a" in output
    assert "stmt 75%" in output
    assert "branch 50%" in output
    assert "RAISE NOTICE 'x'" in output
