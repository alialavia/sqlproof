from __future__ import annotations

import pytest

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.apply import apply_op, prepare_mutants
from sqlproof.mutation.model import Drop, MutationSet, Replace

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
    # plpgsql lazily parses statement bodies, so mangling a statement keyword
    # (e.g. "UPDATE" -> "UPDAT") is not caught by parse_plpgsql.  We instead
    # mangle the block structure itself ("END;" -> "ENDD;"), which causes
    # parse_plpgsql to fail immediately.
    mutations = MutationSet.for_function("bump", [Replace("END;", "ENDD;")])
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
