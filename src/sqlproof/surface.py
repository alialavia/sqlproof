"""SurfaceRegistry: drift detection for the public function surface.

A SurfaceRegistry declares the EXPECTED set of public functions
in a Postgres schema, organized into named sections (helpers,
RPCs, RLS helpers, triggers, …). It compares that expected set
against the LIVE function list and reports drift in both
directions:

  - Functions in the DB but not in the registry are
    ``unexpected`` (most common: a new RPC was added without
    updating tests).
  - Functions in the registry but not in the DB are ``missing``
    (renames and accidental drops).

The canonical usage is one test per project::

    REGISTRY = SurfaceRegistry(
        schema="public",
        sections={
            "pure_helpers": ["extract_domain", ...],
            "business_rpcs": ["create_org_with_owner", ...],
            "rls_helpers": ["current_user_org_ids", ...],
            "triggers": ["assert_org_has_owner", ...],
        },
        exclude_patterns=["graphql_%", "pg_%"],
    )

    def test_no_function_surface_drift(db):
        REGISTRY.assert_no_drift(db)

That single test fails the moment the DB and the test suite
disagree on which functions exist — covering both the
"someone added an untested RPC" and "someone dropped a function
without updating the registry" classes of regression.

The ``db`` argument is any object with a ``.query(sql, *params)``
method returning ``list[dict[str, str]]`` rows — the sqlproof
client shape. Tests can pass any duck-compatible fake.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class _DbClient(Protocol):
    def query(self, sql: str, *params: object) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class DriftReport:
    """Result of a drift comparison.

    ``missing``: registered names with no matching DB function.
    ``unexpected``: DB function names not registered (after
    applying ``exclude_patterns``).
    """

    missing: frozenset[str]
    unexpected: frozenset[str]

    def is_empty(self) -> bool:
        return not self.missing and not self.unexpected


class SurfaceRegistryDrift(AssertionError):
    """Raised by ``assert_no_drift`` when the live function list
    diverges from the registered surface in either direction.
    """


class SurfaceRegistry:
    """Declares the expected function surface for a schema.

    Use a single instance per schema, instantiated at module load
    time and referenced from your test_*_surface.py file.
    """

    def __init__(
        self,
        *,
        schema: str,
        sections: Mapping[str, Iterable[str]],
        exclude_patterns: Iterable[str] = (),
    ) -> None:
        self.schema = schema
        # Materialize sections eagerly — the input might be a
        # generator, and we'll iterate sections more than once.
        self._sections: dict[str, frozenset[str]] = {
            section: frozenset(names) for section, names in sections.items()
        }
        self._exclude_patterns: tuple[str, ...] = tuple(exclude_patterns)

    def expected_names(self) -> frozenset[str]:
        """Flatten every section into one set of expected names."""
        result: set[str] = set()
        for names in self._sections.values():
            result.update(names)
        return frozenset(result)

    def actual_names(self, db: _DbClient) -> frozenset[str]:
        """Query live pg_proc, filter by exclude_patterns.

        Restricted to ``prokind='f'`` so procedures, aggregates,
        and window functions don't leak in.
        """
        rows = db.query(_PG_PROC_SQL, self.schema)
        names = {str(row["proname"]) for row in rows}
        return frozenset(self._filter_excluded(names))

    def drift(self, db: _DbClient) -> DriftReport:
        expected = self.expected_names()
        actual = self.actual_names(db)
        return DriftReport(
            missing=frozenset(expected - actual),
            unexpected=frozenset(actual - expected),
        )

    def assert_no_drift(self, db: _DbClient) -> None:
        """Raises SurfaceRegistryDrift if drift exists in either
        direction. Message includes BOTH the missing AND the
        unexpected sets — fixing only one half is the canonical
        way to ship a second failing CI run.
        """
        report = self.drift(db)
        if report.is_empty():
            return
        lines = [f"Function surface drift in schema {self.schema!r}:"]
        if report.missing:
            lines.append(
                f"  Missing ({len(report.missing)}): "
                f"{sorted(report.missing)}"
            )
        if report.unexpected:
            lines.append(
                f"  Unexpected ({len(report.unexpected)}): "
                f"{sorted(report.unexpected)}"
            )
        raise SurfaceRegistryDrift("\n".join(lines))

    def _filter_excluded(self, names: Iterable[str]) -> set[str]:
        if not self._exclude_patterns:
            return set(names)
        # Compile each SQL LIKE pattern to a regex (anchored at
        # both ends) so we can use re.match — fnmatch's `*`/`?`
        # semantics don't line up cleanly with LIKE's `%`/`_`
        # when literal regex chars are in the pattern.
        compiled = [re.compile(_like_to_regex(p)) for p in self._exclude_patterns]
        return {name for name in names if not any(p.match(name) for p in compiled)}


def _like_to_regex(pattern: str) -> str:
    """Translate a SQL LIKE pattern to an anchored regex.

    LIKE wildcards: ``%`` = any sequence, ``_`` = single char.
    Everything else is escaped to match literally. Anchored at
    both ends so the user-supplied pattern matches the WHOLE
    name, matching SQL LIKE semantics.
    """
    parts: list[str] = []
    for char in pattern:
        if char == "%":
            parts.append(".*")
        elif char == "_":
            parts.append(".")
        else:
            parts.append(re.escape(char))
    parts.append("$")
    return "".join(parts)


# Filter to plain functions (``prokind='f'``) so procedures,
# aggregates, and window functions don't leak in.
_PG_PROC_SQL = """
SELECT p.proname AS proname
FROM pg_catalog.pg_proc p
JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = %s
  AND p.prokind = 'f'
ORDER BY p.proname
"""
