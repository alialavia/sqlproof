from __future__ import annotations

import pytest

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.apply import apply_op, prepare_mutants
from sqlproof.mutation.model import Drop, Mutant, MutationSet, Replace

SCHEMA_SQL = """
CREATE TABLE usage_events (
    id serial PRIMARY KEY,
    user_id integer NOT NULL,
    amount integer NOT NULL
);

CREATE FUNCTION total_usage(p_user integer) RETURNS bigint
LANGUAGE sql STABLE
AS $$
    SELECT COALESCE(SUM(amount), 0) FROM usage_events WHERE user_id = p_user
$$;

CREATE FUNCTION bump(p_id integer) RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE usage_events SET amount = amount + 1 WHERE id = p_id;
END;
$$;
"""


def test_apply_op_replaces_single_occurrence() -> None:
    assert apply_op("a AND b", Replace("AND", "OR")) == "a OR b"


def test_apply_op_drop_deletes_pattern() -> None:
    assert apply_op("SELECT 1 WHERE x", Drop(" WHERE x")) == "SELECT 1"


def test_apply_op_pattern_absent_is_loud() -> None:
    with pytest.raises(SqlProofMutationError, match="not found"):
        apply_op("SELECT 1", Replace("WHERE", "HAVING"))


def test_apply_op_pattern_ambiguous_is_loud() -> None:
    with pytest.raises(SqlProofMutationError, match="2 times"):
        apply_op("a = b AND c = d", Replace("=", "<>"))


def test_prepare_builds_or_replace_ddl_per_mutant() -> None:
    mutations = MutationSet.for_function(
        "total_usage",
        [
            Replace("COALESCE(SUM(amount), 0)", "COALESCE(SUM(amount), 1)"),
            Replace("user_id = p_user", "user_id <> p_user"),
        ],
    )
    prepared = prepare_mutants(mutations, SCHEMA_SQL)
    assert len(prepared) == 2
    assert all("OR REPLACE" in p.ddl.upper() for p in prepared)
    assert "COALESCE(SUM(amount), 1)" in prepared[0].ddl
    assert "user_id <> p_user" in prepared[1].ddl


def test_prepare_validates_sql_body_parses() -> None:
    mutations = MutationSet.for_function(
        "total_usage", [Replace("WHERE user_id", "WHRE user_id")]
    )
    with pytest.raises(SqlProofMutationError, match="does not parse"):
        prepare_mutants(mutations, SCHEMA_SQL)


def test_prepare_rejects_ast_no_op() -> None:
    # Whitespace-only change: different text, identical AST.
    mutations = MutationSet.for_function(
        "total_usage", [Replace("COALESCE(SUM(amount), 0)", "COALESCE( SUM(amount) , 0 )")]
    )
    with pytest.raises(SqlProofMutationError, match="no-op"):
        prepare_mutants(mutations, SCHEMA_SQL)


def test_prepare_validates_plpgsql_body() -> None:
    mutations = MutationSet.for_function("bump", [Replace("UPDATE", "UPDAT")])
    with pytest.raises(SqlProofMutationError, match="does not parse"):
        prepare_mutants(mutations, SCHEMA_SQL)


def test_prepare_accepts_valid_plpgsql_mutant() -> None:
    mutations = MutationSet.for_function("bump", [Replace("amount + 1", "amount + 2")])
    (prepared,) = prepare_mutants(mutations, SCHEMA_SQL)
    assert "amount + 2" in prepared.ddl


def test_mutant_ids_are_stable_and_distinct() -> None:
    mutations = MutationSet.for_function(
        "total_usage",
        [
            Replace("user_id = p_user", "user_id <> p_user"),
            Replace("COALESCE(SUM(amount), 0)", "COALESCE(SUM(amount), 1)"),
        ],
    )
    first = prepare_mutants(mutations, SCHEMA_SQL)
    second = prepare_mutants(mutations, SCHEMA_SQL)
    assert [p.mutant_id for p in first] == [p.mutant_id for p in second]
    assert len({p.mutant_id for p in first}) == 2


def test_duplicate_mutants_are_rejected() -> None:
    # Two ops that produce the identical mutated AST.
    mutations = MutationSet.for_function(
        "total_usage",
        [
            Replace("user_id = p_user", "user_id <> p_user"),
            Replace("= p_user", "<> p_user"),
        ],
    )
    with pytest.raises(SqlProofMutationError, match="duplicate"):
        prepare_mutants(mutations, SCHEMA_SQL)


# ---------------------------------------------------------------------------
# Fix 1: Wrap original-body parse failures
# ---------------------------------------------------------------------------

def test_prepare_raises_on_broken_original_sql_body() -> None:
    """A function whose *original* body is invalid SQL must raise, mentioning 'original'."""
    schema_with_bad_body = """
CREATE TABLE t (id serial PRIMARY KEY);

CREATE FUNCTION broken(p integer) RETURNS integer
LANGUAGE sql STABLE
AS $$ SELEC 1 $$;
"""
    mutations = MutationSet.for_function("broken", [Replace("SELEC", "SELECT")])
    with pytest.raises(SqlProofMutationError, match="original"):
        prepare_mutants(mutations, schema_with_bad_body)


# ---------------------------------------------------------------------------
# Fix 2: Attach mutant context to op failures
# ---------------------------------------------------------------------------

def test_prepare_op_failure_includes_function_name() -> None:
    """When an op pattern is not found, the error should include the function name."""
    mutations = MutationSet.for_function("total_usage", [Replace("NOPE", "X")])
    with pytest.raises(SqlProofMutationError, match="total_usage"):
        prepare_mutants(mutations, SCHEMA_SQL)


# ---------------------------------------------------------------------------
# Fix 3 + 4: Id stability under reformatting (sql and plpgsql)
# ---------------------------------------------------------------------------

# Schema variant with extra whitespace inside the total_usage body
SCHEMA_SQL_REFORMATTED_SQL = """
CREATE TABLE usage_events (
    id serial PRIMARY KEY,
    user_id integer NOT NULL,
    amount integer NOT NULL
);

CREATE FUNCTION total_usage(p_user integer) RETURNS bigint
LANGUAGE sql STABLE
AS $$
    SELECT   COALESCE(SUM(amount), 0)   FROM usage_events   WHERE user_id = p_user
$$;

CREATE FUNCTION bump(p_id integer) RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE usage_events SET amount = amount + 1 WHERE id = p_id;
END;
$$;
"""

# Schema variant with an extra newline inside the bump body
SCHEMA_SQL_REFORMATTED_PLPGSQL = """
CREATE TABLE usage_events (
    id serial PRIMARY KEY,
    user_id integer NOT NULL,
    amount integer NOT NULL
);

CREATE FUNCTION total_usage(p_user integer) RETURNS bigint
LANGUAGE sql STABLE
AS $$
    SELECT COALESCE(SUM(amount), 0) FROM usage_events WHERE user_id = p_user
$$;

CREATE FUNCTION bump(p_id integer) RETURNS void
LANGUAGE plpgsql
AS $$
BEGIN

    UPDATE usage_events SET amount = amount + 1 WHERE id = p_id;
END;
$$;
"""


def test_sql_mutant_id_stable_under_whitespace_reformatting() -> None:
    """Same Replace op on two schemas differing only in sql body whitespace → same mutant_id."""
    op = Replace("user_id = p_user", "user_id <> p_user")
    mutations = MutationSet.for_function("total_usage", [op])
    (p1,) = prepare_mutants(mutations, SCHEMA_SQL)
    (p2,) = prepare_mutants(mutations, SCHEMA_SQL_REFORMATTED_SQL)
    assert p1.mutant_id == p2.mutant_id


def test_plpgsql_mutant_id_stable_under_whitespace_reformatting() -> None:
    """Same Replace op on two schemas differing only in plpgsql body whitespace → same mutant_id."""
    op = Replace("amount + 1", "amount + 2")
    mutations = MutationSet.for_function("bump", [op])
    (p1,) = prepare_mutants(mutations, SCHEMA_SQL)
    (p2,) = prepare_mutants(mutations, SCHEMA_SQL_REFORMATTED_PLPGSQL)
    assert p1.mutant_id == p2.mutant_id


# ---------------------------------------------------------------------------
# Fix 5: Additional high-value tests
# ---------------------------------------------------------------------------

def test_multi_op_mutant_applies_all_ops() -> None:
    """A Mutant with two ops should produce a PreparedMutant reflecting both changes."""
    mutant = Mutant(
        target_kind="function",
        target_name="total_usage",
        ops=(
            Replace("user_id = p_user", "user_id <> p_user"),
            Replace("COALESCE(SUM(amount), 0)", "COALESCE(SUM(amount), 1)"),
        ),
    )
    mutation_set = MutationSet((mutant,))
    (prepared,) = prepare_mutants(mutation_set, SCHEMA_SQL)
    assert "user_id <> p_user" in prepared.ddl
    assert "COALESCE(SUM(amount), 1)" in prepared.ddl


def test_drop_op_removes_where_clause() -> None:
    """Drop op on total_usage removes the WHERE clause from the prepared DDL."""
    mutations = MutationSet.for_function(
        "total_usage", [Drop(" WHERE user_id = p_user")]
    )
    (prepared,) = prepare_mutants(mutations, SCHEMA_SQL)
    assert "WHERE" not in prepared.ddl


_SCHEMA_SQL_UNKNOWN_LANG = """
CREATE TABLE t (id serial PRIMARY KEY);

CREATE FUNCTION py() RETURNS integer LANGUAGE plpython3u
AS $$ return 1 $$;
"""


def test_unknown_language_replace_prepares_fine() -> None:
    """A function in an unknown language (plpython3u) can still be mutated via text replacement."""
    mutations = MutationSet.for_function("py", [Replace("return 1", "return 2")])
    (prepared,) = prepare_mutants(mutations, _SCHEMA_SQL_UNKNOWN_LANG)
    assert "return 2" in prepared.ddl


def test_unknown_language_whitespace_only_replace_is_noop() -> None:
    """A whitespace-only change in an unknown language body is detected as a no-op."""
    mutations = MutationSet.for_function("py", [Replace("return 1", "return  1")])
    with pytest.raises(SqlProofMutationError, match="no-op"):
        prepare_mutants(mutations, _SCHEMA_SQL_UNKNOWN_LANG)
