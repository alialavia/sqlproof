"""SurfaceRegistry: drift detection between an expected function
surface and the live database (#12).

Every sqlproof project of meaningful size ends up rolling its own
``test_current_public_surface.py`` that lists expected functions
and asserts ``actual == expected``. This module gives that
pattern a first-class home: declare your sections (helpers,
business RPCs, RLS helpers, triggers), and call
``registry.assert_no_drift(db)`` in a single test.

Invariants pinned down here:

  (i) ``expected_names()`` flattens every section into one set —
      duplicates across sections (which shouldn't happen in
      practice but are technically possible) are de-duplicated.
  (ii) ``drift(db)`` correctly classifies functions that exist in
       the DB but not in the registry as ``unexpected``, and
       functions in the registry but not in the DB as
       ``missing``.
  (iii) ``exclude_patterns`` (SQL LIKE-style wildcards) filter
        the live function list before comparison — so
        ``graphql%`` or ``pg_%`` system functions don't drown
        out drift signal.
  (iv) ``assert_no_drift(db)`` raises ``SurfaceRegistryDrift``
       with a message identifying the missing AND unexpected
       sets (not just one side), so users see both classes of
       error in a single CI failure.

Failure cases each invariant addresses:
  (i): Two sections accidentally containing the same function
       name (e.g. moving a function between sections without
       cleanup) would inflate expected_names if naive
       concatenation were used.
  (ii): A drift detector that only reports one direction (e.g.
        only "missing") would miss the most common case: a new
        RPC was added without a matching test, so it appears as
        an unexpected function.
  (iii): A "drift" report dominated by 200 system functions
         (graphql_*, pgcrypto, pg_*) buries the actual signal.
  (iv): A user who fixes the "missing" half without seeing the
        "unexpected" half ships a half-fix and lands another
        failing CI run.
"""

from __future__ import annotations

import pytest

from sqlproof.surface import SurfaceRegistry, SurfaceRegistryDrift


class _FakeDb:
    """Stand-in for a sqlproof client that satisfies the
    `.query(sql)` interface used by SurfaceRegistry. Returns
    canned ``proname`` rows for the pg_proc query.
    """

    def __init__(self, function_names: list[str]) -> None:
        self._function_names = function_names

    def query(self, _sql: str, *_params: object) -> list[dict[str, str]]:
        return [{"proname": name} for name in self._function_names]


# ---------------------------------------------------------------------------
# Invariant (i): expected_names flattens sections, de-duplicates
# ---------------------------------------------------------------------------


def test_expected_names_flattens_sections() -> None:
    registry = SurfaceRegistry(
        schema="public",
        sections={
            "rpcs": ["create_org", "get_visibility"],
            "rls_helpers": ["current_user_orgs"],
        },
    )
    assert registry.expected_names() == {
        "create_org",
        "get_visibility",
        "current_user_orgs",
    }


def test_expected_names_dedupes_across_sections() -> None:
    """Invariant (i): if the same name appears in two sections
    (degenerate but possible), the flattened set has one entry."""
    registry = SurfaceRegistry(
        schema="public",
        sections={
            "rpcs": ["shared_fn"],
            "rls_helpers": ["shared_fn"],
        },
    )
    assert registry.expected_names() == {"shared_fn"}


# ---------------------------------------------------------------------------
# Invariant (ii): drift classifies missing AND unexpected functions
# ---------------------------------------------------------------------------


def test_drift_reports_empty_when_actual_matches_expected() -> None:
    registry = SurfaceRegistry(
        schema="public",
        sections={"rpcs": ["create_org", "get_visibility"]},
    )
    db = _FakeDb(["create_org", "get_visibility"])
    report = registry.drift(db)
    assert report.missing == frozenset()
    assert report.unexpected == frozenset()


def test_drift_reports_new_function_as_unexpected() -> None:
    """Invariant (ii): a function in the DB that isn't in the
    registry shows up as ``unexpected`` — the canonical 'someone
    added an RPC without updating the test' case."""
    registry = SurfaceRegistry(
        schema="public",
        sections={"rpcs": ["create_org"]},
    )
    db = _FakeDb(["create_org", "create_brand"])
    report = registry.drift(db)
    assert report.missing == frozenset()
    assert report.unexpected == frozenset({"create_brand"})


def test_drift_reports_dropped_function_as_missing() -> None:
    """Invariant (ii): a function in the registry but not in the
    DB shows up as ``missing`` — covers renames and accidental
    drops."""
    registry = SurfaceRegistry(
        schema="public",
        sections={"rpcs": ["create_org", "renamed_fn"]},
    )
    db = _FakeDb(["create_org"])
    report = registry.drift(db)
    assert report.missing == frozenset({"renamed_fn"})
    assert report.unexpected == frozenset()


# ---------------------------------------------------------------------------
# Invariant (iii): exclude_patterns filter system functions out
# ---------------------------------------------------------------------------


def test_exclude_patterns_filter_live_function_list() -> None:
    """Invariant (iii): live functions matching any exclude
    pattern aren't counted toward drift. Without this, every
    Supabase project would see hundreds of graphql_* and
    pg_* functions as 'unexpected'."""
    registry = SurfaceRegistry(
        schema="public",
        sections={"rpcs": ["create_org"]},
        exclude_patterns=["graphql_%", "pg_%"],
    )
    db = _FakeDb(
        [
            "create_org",
            "graphql_resolve",
            "graphql_field",
            "pg_get_serial_sequence",
        ]
    )
    report = registry.drift(db)
    assert report.missing == frozenset()
    assert report.unexpected == frozenset()


def test_exclude_patterns_use_sql_like_wildcards() -> None:
    """LIKE-style wildcards: ``%`` matches any sequence, ``_``
    matches a single character. Matching the SQL convention
    keeps the patterns coherent with what the user would write
    if they queried pg_proc directly."""
    registry = SurfaceRegistry(
        schema="public",
        sections={"rpcs": ["create_org"]},
        exclude_patterns=["test__"],  # two underscores: matches 'testXY' (6-char names)
    )
    db = _FakeDb(["create_org", "test12", "testABCDE"])
    report = registry.drift(db)
    # 'test12' matches 'test__' (6 chars, 4 literals + 2 wildcards).
    # 'testABCDE' is too long, so it's reported as unexpected.
    assert report.unexpected == frozenset({"testABCDE"})


# ---------------------------------------------------------------------------
# Invariant (iv): assert_no_drift surfaces BOTH sides in the error
# ---------------------------------------------------------------------------


def test_assert_no_drift_passes_when_aligned() -> None:
    registry = SurfaceRegistry(
        schema="public",
        sections={"rpcs": ["create_org"]},
    )
    db = _FakeDb(["create_org"])
    registry.assert_no_drift(db)  # does not raise


def test_assert_no_drift_raises_with_missing_and_unexpected() -> None:
    """Invariant (iv): the error message names both sets so a
    user reading a CI failure sees the full picture, not just
    half of it."""
    registry = SurfaceRegistry(
        schema="public",
        sections={"rpcs": ["create_org", "renamed_fn"]},
    )
    db = _FakeDb(["create_org", "new_fn"])
    with pytest.raises(SurfaceRegistryDrift) as exc:
        registry.assert_no_drift(db)
    message = str(exc.value)
    assert "renamed_fn" in message, f"missing set should appear in message: {message}"
    assert "new_fn" in message, f"unexpected set should appear in message: {message}"
