"""PL/pgSQL coverage via the `plpgsql_check` extension.

Two entry points are provided:

1. ``coverage_session`` (recommended) — a high-level context manager that
   wraps the common pattern of "drive a known set of RPCs through a test,
   then check that each of them got non-zero coverage." It handles
   profiler-GUC enablement, language filtering, drift logging,
   skip-on-missing-extension, and yields ``(report, installed)``::

       from sqlproof.contrib.plpgsql_coverage import (
           assert_nonzero_coverage,
           coverage_session,
           drive_in_order,
       )

       BRAND_RPCS = ["get_brand_visibility_stats", ...]

       with proof.client_for_dataset({}) as db:
           with coverage_session(db, BRAND_RPCS, cluster="brand") as (report, installed):
               drive_in_order(installed, drivers, cluster="brand")
       print(report.format())
       assert_nonzero_coverage(report, installed, cluster="brand")

2. ``collect_coverage`` (low-level primitive) — context manager that resets
   the profiler counters on enter and reads back per-function data on exit.
   Useful when you're driving a state machine that doesn't fit the
   "iterate a drivers dict" shape of ``coverage_session``::

       from sqlproof.contrib.plpgsql_coverage import collect_coverage

       with collect_coverage(db, functions=["my_func"]) as report:
           proof.run_state_machine(MyMachine, ...)
       print(report.format())

The ``plpgsql_check`` extension must be installed in the target database::

    CREATE EXTENSION IF NOT EXISTS plpgsql_check;

If it is absent, ``coverage_session`` calls ``pytest.skip`` by default
(opt out via ``skip_on_missing_extension=False``). ``collect_coverage``
raises :class:`PlpgsqlCheckNotAvailable` directly.

Profiler scope
--------------
Both entry points target PL/pgSQL functions only — ``plpgsql_check`` cannot
profile ``LANGUAGE sql`` (or any other language) function bodies. Names
passed in ``candidates`` / ``functions=`` are intersected with installed
PL/pgSQL functions; non-PL/pgSQL names are silently filtered out.
``coverage_session`` additionally logs a drift line when names are missing
or non-PL/pgSQL so the divergence is visible.
"""

from __future__ import annotations

from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlproof.client import SqlProofClient


class PlpgsqlCheckNotAvailable(Exception):
    """Raised when the `plpgsql_check` extension is not installed."""


@dataclass(frozen=True)
class LineCoverage:
    lineno: int
    source: str
    exec_count: int  # 0 = not hit


@dataclass
class FunctionCoverage:
    name: str
    lines: list[LineCoverage] = field(default_factory=list[LineCoverage])
    statement_ratio: float = 0.0
    branch_ratio: float = 0.0

    @property
    def hit_lines(self) -> int:
        return sum(1 for ln in self.lines if ln.exec_count > 0)

    @property
    def total_executable_lines(self) -> int:
        """Lines that plpgsql_check considers executable (have an exec_stmts entry)."""
        return len(self.lines)


@dataclass
class PlpgsqlCoverageReport:
    functions: dict[str, FunctionCoverage] = field(
        default_factory=dict[str, FunctionCoverage]
    )

    def statement_coverage(self, name: str) -> float:
        """Statement coverage ratio for a single function (0.0-1.0)."""
        fc = self.functions.get(name)
        return fc.statement_ratio if fc is not None else 0.0

    def branch_coverage(self, name: str) -> float:
        """Branch coverage ratio for a single function (0.0-1.0)."""
        fc = self.functions.get(name)
        return fc.branch_ratio if fc is not None else 0.0

    def format(self, *, show_source: bool = True) -> str:
        """Render a human-readable coverage report."""
        if not self.functions:
            return "No PL/pgSQL functions tracked."

        lines: list[str] = []
        for name, fc in sorted(self.functions.items()):
            stmt_pct = fc.statement_ratio * 100
            branch_pct = fc.branch_ratio * 100
            lines.append(
                f"\n{name}  "
                f"stmt {stmt_pct:.0f}%  branch {branch_pct:.0f}%  "
                f"({fc.hit_lines}/{fc.total_executable_lines} executable lines)"
            )
            if show_source and fc.lines:
                lines.append("  " + "-" * 60)
                for ln in fc.lines:
                    marker = ">" if ln.exec_count > 0 else " "
                    count = f"{ln.exec_count:>4}" if ln.exec_count > 0 else "    "
                    lines.append(f"  {marker} {ln.lineno:>4} {count}  {ln.source}")

        total_funcs = len(self.functions)
        fully_covered = sum(
            1 for fc in self.functions.values() if fc.statement_ratio >= 1.0
        )
        lines.insert(
            0,
            f"PL/pgSQL coverage: {fully_covered}/{total_funcs} functions fully covered",
        )
        return "\n".join(lines)


def plpgsql_check_available(db: SqlProofClient) -> bool:
    """Return True if `plpgsql_check` is installed in the database."""
    rows = db.query(
        "SELECT 1 FROM pg_extension WHERE extname = 'plpgsql_check'"
    )
    return bool(rows)


def installed_plpgsql_functions(
    db: SqlProofClient,
    candidates: Iterable[str] | None = None,
    *,
    schema: str = "public",
) -> set[str]:
    """Return the set of PL/pgSQL function names installed in `schema`.

    If `candidates` is given, the returned set is intersected with it —
    only names that are *both* in `candidates` *and* installed as
    PL/pgSQL functions in `schema` are returned. Names in `candidates`
    that are missing or implemented in another language (e.g.
    ``LANGUAGE sql``) are silently dropped.

    Use this to filter a registry-style list of expected RPCs against
    the live database — non-PL/pgSQL functions can't be profiled by
    ``plpgsql_check`` and including them in ``functions=`` for
    ``collect_coverage`` would corrupt the report.

    Args:
        db: A live `SqlProofClient`.
        candidates: Optional list/set of names to intersect with the
            installed PL/pgSQL set. ``None`` (default) returns all
            installed PL/pgSQL functions in `schema`.
        schema: Schema to scan. Defaults to ``"public"``.

    Returns:
        A set of function names (no signatures, no overload disambig).
    """
    if candidates is None:
        rows = db.query(
            """
            SELECT p.proname AS name
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            JOIN pg_language l ON l.oid = p.prolang
            WHERE n.nspname = %s AND l.lanname = 'plpgsql'
            """,
            schema,
        )
        return {row["name"] for row in rows}

    candidate_list = list(candidates)
    if not candidate_list:
        return set()

    rows = db.query(
        """
        SELECT p.proname AS name
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        JOIN pg_language l ON l.oid = p.prolang
        WHERE n.nspname = %s
          AND l.lanname = 'plpgsql'
          AND p.proname = ANY(%s)
        """,
        schema,
        candidate_list,
    )
    return {row["name"] for row in rows}


def _collect_function_coverage(
    db: SqlProofClient, name: str
) -> FunctionCoverage | None:
    """Read per-line profiler data and aggregate ratios for one function."""
    try:
        rows = db.query(
            "SELECT lineno, exec_stmts, source "
            "FROM plpgsql_profiler_function_tb(%s::text)",
            name,
        )
    except Exception:
        return None

    lines: list[LineCoverage] = []
    for row in rows:
        source: str = row.get("source") or ""
        exec_count: int = int(row["exec_stmts"] or 0)
        lineno: int = int(row["lineno"])
        # Only include lines that the profiler considers executable
        # (exec_stmts column is non-NULL). Lines like DECLARE / BEGIN
        # are structural, not executable — exclude them from the report.
        if row["exec_stmts"] is not None:
            lines.append(LineCoverage(lineno=lineno, source=source, exec_count=exec_count))

    try:
        stmt_rows = db.query(
            "SELECT plpgsql_coverage_statements(%s::text) AS ratio", name
        )
        stmt_ratio = float(stmt_rows[0]["ratio"] or 0.0) if stmt_rows else 0.0
    except Exception:
        stmt_ratio = 0.0

    try:
        branch_rows = db.query(
            "SELECT plpgsql_coverage_branches(%s::text) AS ratio", name
        )
        branch_ratio = float(branch_rows[0]["ratio"] or 0.0) if branch_rows else 0.0
    except Exception:
        branch_ratio = 0.0

    return FunctionCoverage(
        name=name,
        lines=lines,
        statement_ratio=stmt_ratio,
        branch_ratio=branch_ratio,
    )


@contextmanager
def collect_coverage(
    db: SqlProofClient,
    functions: Iterable[str] | None = None,
    *,
    schema: str = "public",
) -> Generator[PlpgsqlCoverageReport]:
    """Context manager that collects PL/pgSQL coverage over a test block.

    On enter:
      * Verifies the `plpgsql_check` extension is installed.
      * Enables the ``plpgsql_check.profiler`` GUC (off by default;
        without it, profiler reads return rows where every
        ``exec_stmts`` is NULL, which look like 0% coverage).
      * Resets all profiler counters.

    On exit:
      * Reads per-line data for each tracked function and populates the
        returned ``PlpgsqlCoverageReport``.

    The candidate list is filtered to PL/pgSQL functions in `schema`.
    Non-PL/pgSQL or missing names are silently skipped — including a
    ``LANGUAGE sql`` function in ``functions=`` previously corrupted the
    report for any function that came after it in iteration order.

    Args:
        db: A live `SqlProofClient` (must have `plpgsql_check` installed).
        functions: Function names to include in the report. If None,
            all PL/pgSQL functions in `schema` are tracked.
        schema: Schema to scan. Defaults to ``"public"``.

    Raises:
        PlpgsqlCheckNotAvailable: if the extension is not installed.
    """
    if not plpgsql_check_available(db):
        raise PlpgsqlCheckNotAvailable(
            "plpgsql_check extension is not installed. "
            "Run: CREATE EXTENSION IF NOT EXISTS plpgsql_check;"
        )

    # Enable the profiler GUC. `plpgsql_check.profiler` defaults to off,
    # so without this, `plpgsql_profiler_function_tb` returns rows where
    # `exec_stmts` is always NULL — looking like 0% coverage even for
    # functions that ran successfully. SET (not SET LOCAL) so the GUC
    # survives savepoint rollback inside the test block.
    db.execute("SET plpgsql_check.profiler = on")

    db.execute("SELECT plpgsql_profiler_reset_all()")

    report = PlpgsqlCoverageReport()
    try:
        yield report
    finally:
        installed = installed_plpgsql_functions(db, functions, schema=schema)
        for name in sorted(installed):
            fc = _collect_function_coverage(db, name)
            if fc is not None:
                report.functions[name] = fc


@contextmanager
def coverage_session(
    db: SqlProofClient,
    candidates: Iterable[str],
    *,
    cluster: str,
    skip_on_missing_extension: bool = True,
    skip_on_no_installed: bool = True,
    schema: str = "public",
) -> Generator[tuple[PlpgsqlCoverageReport, set[str]]]:
    """High-level coverage context manager for a named cluster of RPCs.

    This is the recommended entry point for "drive a known set of public
    functions and check each got non-zero coverage" — the most common
    coverage-test shape. It wraps :func:`collect_coverage` and adds:

      * **Drift logging.** Names that are missing from the live DB or
        not PL/pgSQL are listed in a ``[<cluster>-coverage]`` log line so
        the divergence is visible.
      * **Skip on missing extension.** If ``plpgsql_check`` isn't
        installed, calls :func:`pytest.skip` with a hint pointing at the
        install command. Set ``skip_on_missing_extension=False`` to
        propagate :class:`PlpgsqlCheckNotAvailable` instead.
      * **Skip on empty installed set.** If none of ``candidates`` are
        installed PL/pgSQL functions (drift, all SQL-language, etc.),
        calls :func:`pytest.skip` rather than passing vacuously. Set
        ``skip_on_no_installed=False`` to receive an empty ``installed``
        set instead.

    Yields ``(report, installed)``. Drive each function in ``installed``
    inside the ``with`` block (typically via :func:`drive_in_order`),
    then read ``report`` after.

    Args:
        db: A live `SqlProofClient`.
        candidates: Names of expected PL/pgSQL functions to cover. Names
            that are missing or non-PL/pgSQL are filtered out.
        cluster: Short label used in drift / skip log messages, e.g.
            ``"brand"``, ``"billing"``, ``"orgs"``. Lets a multi-cluster
            test run produce greppable per-cluster output.
        skip_on_missing_extension: If True (default), call
            ``pytest.skip`` when ``plpgsql_check`` isn't installed.
        skip_on_no_installed: If True (default), call ``pytest.skip``
            when no candidates are installed PL/pgSQL functions.
        schema: Schema to scan. Defaults to ``"public"``.

    Yields:
        ``(report, installed)`` — the populated-on-exit
        :class:`PlpgsqlCoverageReport` and the resolved set of installed
        PL/pgSQL function names.
    """
    candidate_set = set(candidates)

    if not plpgsql_check_available(db):
        if skip_on_missing_extension:
            import pytest

            pytest.skip(
                "plpgsql_check extension not installed; "
                "run `CREATE EXTENSION IF NOT EXISTS plpgsql_check` "
                "in the target database to enable coverage reports."
            )
        raise PlpgsqlCheckNotAvailable(
            "plpgsql_check extension is not installed. "
            "Run: CREATE EXTENSION IF NOT EXISTS plpgsql_check;"
        )

    installed = installed_plpgsql_functions(db, candidate_set, schema=schema)
    missing = sorted(candidate_set - installed)
    if missing:
        print(
            f"[{cluster}-coverage] schema/DB drift — these candidates are missing "
            f"from the live DB or are not LANGUAGE plpgsql in schema {schema!r} "
            f"(plpgsql_check cannot profile non-plpgsql bodies): {missing}"
        )

    if not installed:
        if skip_on_no_installed:
            import pytest

            pytest.skip(
                f"no PL/pgSQL functions to profile in {cluster} cluster — "
                f"all candidates are missing from {schema!r} or "
                f"implemented in a non-plpgsql language."
            )
        # Yield an empty report + empty set; let the caller decide.
        yield PlpgsqlCoverageReport(), set()
        return

    with collect_coverage(db, functions=installed, schema=schema) as report:
        yield report, installed


def drive_in_order(
    installed: Iterable[str],
    drivers: dict[str, Callable[[], Any]],
    *,
    cluster: str,
) -> None:
    """Invoke each function in `installed` exactly once via `drivers`.

    Iterates ``installed`` in sorted order so coverage runs are
    reproducible across processes (Python set iteration order is
    insertion-dependent and won't match between runs). Logs which call
    raised before re-raising so a single broken driver doesn't make the
    whole cluster look mysteriously empty.

    Args:
        installed: Function names to drive (typically the ``installed``
            set yielded by :func:`coverage_session`).
        drivers: Mapping from function name to a zero-arg callable that
            exercises that function with realistic seeded data. Each
            callable is invoked exactly once.
        cluster: Short label used in error log messages, matched to the
            label used at :func:`coverage_session` entry.

    Raises:
        KeyError: with a clear "missing driver" message if `installed`
            contains a name not in `drivers`. This typically means the
            cluster's drivers dict and ``candidates`` list have drifted.
        Exception: re-raises whatever a driver raised, after logging
            which one failed.
    """
    for fn_name in sorted(installed):
        try:
            driver = drivers[fn_name]
        except KeyError:
            raise KeyError(
                f"[{cluster}-coverage] no driver registered for {fn_name!r}. "
                f"Add it to the drivers dict in this cluster's coverage test, "
                f"or remove it from the candidates list."
            ) from None

        try:
            driver()
        except Exception as exc:
            print(
                f"[{cluster}-coverage] driving {fn_name} raised "
                f"{type(exc).__name__}: {exc}"
            )
            raise


def assert_nonzero_coverage(
    report: PlpgsqlCoverageReport,
    installed: Iterable[str],
    *,
    cluster: str,
    threshold: float = 0.0,
) -> None:
    """Assert every function in `installed` got > `threshold` statement coverage.

    The default ``threshold=0.0`` is a smoke check: every named function
    was invoked at least once. Raise the threshold per-cluster as drivers
    grow more thorough — e.g. ``threshold=0.8`` to enforce 80% statement
    coverage across all installed functions.

    Args:
        report: The :class:`PlpgsqlCoverageReport` populated by
            :func:`coverage_session` / :func:`collect_coverage`.
        installed: Function names that were expected to be covered.
        cluster: Short label used in the assertion message.
        threshold: Minimum statement coverage ratio (0.0-1.0). Default
            0.0 means "any execution at all".

    Raises:
        AssertionError: lists every function whose statement coverage is
            at or below `threshold`.
    """
    below = sorted(
        fn for fn in installed if report.statement_coverage(fn) <= threshold
    )
    if below:
        raise AssertionError(
            f"installed {cluster} functions below "
            f"{threshold:.0%} statement coverage: {below}"
        )
