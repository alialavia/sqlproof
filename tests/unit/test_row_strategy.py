"""``SqlProof.row_strategy`` — schema-backed single-row builder for
ad-hoc test setup (issue #13).

The fixture/helper pattern of hand-rolled ``INSERT INTO X (a, b, c)
VALUES (...)`` strings drifts silently from the live schema: when a
migration adds a NOT NULL column, the helper compiles fine and only
fails at runtime, in a test that's typically not about the changed
table.

The fix isn't a linter for those INSERTs — it's to stop hand-rolling
them. ``row_strategy`` exposes the existing schema-aware row
generator (``sqlproof.generators.rows.table_rows_strategy``) under a
friendlier name, sized for the single-row case, namespaced to a
specific table, with overrides passed as kwargs. The output is a
Hypothesis ``SearchStrategy[dict]`` ready to feed into a
``@given``-decorated test or an explicit ``.example()`` call for
ad-hoc fixture data.

The point: when the schema gains a column, ``row_strategy`` callers
automatically receive a valid value for it. Hand-rolled INSERTs
don't get that for free.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from sqlproof.core import SqlProof
from sqlproof.exceptions import SqlProofSchemaError


def _proof_with_projects_schema() -> SqlProof:
    """Schema with a NOT NULL column the test fixtures must satisfy."""
    sql = """
        CREATE TABLE users (
            id serial PRIMARY KEY,
            email text NOT NULL
        );
        CREATE TABLE projects (
            id serial PRIMARY KEY,
            user_id integer NOT NULL REFERENCES users(id),
            name text NOT NULL,
            org_id integer NOT NULL
        );
    """
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkstemp(suffix=".sql")[1])
    tmp.write_text(sql, encoding="utf-8")
    return SqlProof.from_schema_file(tmp)


def test_row_strategy_returns_search_strategy_of_dict() -> None:
    """Single-row strategy yields one dict, not a list. Distinct from
    ``dataset_strategy`` (which returns a dict-of-table-to-list) and
    ``table_rows_strategy`` (which returns a list)."""
    proof = _proof_with_projects_schema()

    strategy = proof.row_strategy("users")

    assert isinstance(strategy, SearchStrategy)

    @given(strategy)
    def assert_dict_shape(row: object) -> None:
        assert isinstance(row, dict)
        assert "id" in row
        assert "email" in row

    assert_dict_shape()


def test_row_strategy_populates_not_null_columns() -> None:
    """Every NOT NULL column on the table appears in the row with a
    non-None value. This is the load-bearing property: a migration
    that adds a NOT NULL column gets picked up automatically.

    Real fixtures pass an explicit FK value (the user-id you just
    created in a parent fixture). ``org_id`` here stands in for the
    "migration just added this NOT NULL column" case — no override is
    given, but the generator fills it.
    """
    proof = _proof_with_projects_schema()

    @given(proof.row_strategy("projects", user_id=42))
    def assert_not_null_populated(row: dict[str, object]) -> None:
        assert row["id"] is not None
        assert row["user_id"] == 42
        assert row["name"] is not None
        assert row["org_id"] is not None  # newly-added NOT NULL — auto-filled.

    assert_not_null_populated()


def test_row_strategy_override_bare_value() -> None:
    """Overrides accept bare values (no ``st.just`` wrapping required).
    Existing ``_draw_override`` in generators/rows.py already supports
    this; we just need the wrapper to forward it."""
    proof = _proof_with_projects_schema()

    @given(proof.row_strategy("projects", user_id=1, name="acme"))
    def assert_override_applied(row: dict[str, object]) -> None:
        assert row["name"] == "acme"

    assert_override_applied()


def test_row_strategy_override_strategy() -> None:
    """Overrides accept Hypothesis strategies. Each draw uses a fresh
    value from the strategy."""
    proof = _proof_with_projects_schema()

    @given(
        proof.row_strategy(
            "projects",
            user_id=1,
            name=st.sampled_from(["a", "b", "c"]),
        )
    )
    def assert_override_in_set(row: dict[str, object]) -> None:
        assert row["name"] in {"a", "b", "c"}

    assert_override_in_set()


def test_row_strategy_unknown_table_raises() -> None:
    """Bad table name fails loudly at strategy-construction time, not
    deep inside a draw."""
    proof = _proof_with_projects_schema()

    with pytest.raises(SqlProofSchemaError):
        proof.row_strategy("nonexistent_table")


def test_row_strategy_unknown_column_override_raises() -> None:
    """Typo in an override key fails loudly. Without this check a
    misspelled override would silently no-op (the generator would
    invent its own value) — exactly the kind of silent drift this
    helper exists to prevent."""
    from sqlproof.exceptions import SqlProofUsageError

    proof = _proof_with_projects_schema()

    with pytest.raises(SqlProofUsageError, match="unknown column"):
        proof.row_strategy("projects", user_id=1, naem="typo")  # 'naem' typo


def test_row_strategy_respects_check_constraints() -> None:
    """A CHECK constraint refines the generated value. Same engine as
    ``table_rows_strategy``; verifying the wrapper doesn't bypass it."""
    sql = """
        CREATE TABLE accounts (
            id serial PRIMARY KEY,
            balance integer NOT NULL CHECK (balance >= 0)
        );
    """
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkstemp(suffix=".sql")[1])
    tmp.write_text(sql, encoding="utf-8")
    proof = SqlProof.from_schema_file(tmp)

    @given(proof.row_strategy("accounts"))
    def assert_balance_non_negative(row: dict[str, object]) -> None:
        balance = row["balance"]
        assert isinstance(balance, int)
        assert balance >= 0

    assert_balance_non_negative()
