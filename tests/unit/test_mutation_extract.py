from __future__ import annotations

import pytest

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.extract import build_mutated_ddl, extract_function

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


def test_extracts_sql_function_body_and_language() -> None:
    source = extract_function(SCHEMA_SQL, "total_usage")
    assert source.name == "total_usage"
    assert source.language == "sql"
    assert "COALESCE(SUM(amount), 0)" in source.body
    assert "WHERE user_id = p_user" in source.body


def test_extracts_plpgsql_function() -> None:
    source = extract_function(SCHEMA_SQL, "bump")
    assert source.language == "plpgsql"
    assert "amount + 1" in source.body


def test_ddl_is_a_deparsed_create_statement() -> None:
    source = extract_function(SCHEMA_SQL, "total_usage")
    assert source.ddl.upper().startswith("CREATE FUNCTION")
    assert "total_usage" in source.ddl


def test_missing_function_raises() -> None:
    with pytest.raises(SqlProofMutationError, match="no_such_function"):
        extract_function(SCHEMA_SQL, "no_such_function")


def test_overloaded_function_raises_ambiguous() -> None:
    overloaded = (
        SCHEMA_SQL
        + "\nCREATE FUNCTION total_usage(p_user integer, p_cap integer) RETURNS bigint"
        + "\nLANGUAGE sql AS $$ SELECT 1 $$;"
    )
    with pytest.raises(SqlProofMutationError, match="2 definitions"):
        extract_function(overloaded, "total_usage")


def test_unparseable_schema_raises() -> None:
    with pytest.raises(SqlProofMutationError, match="does not parse"):
        extract_function("CREATE FUNCTION oops(", "oops")


def test_begin_atomic_body_is_rejected_for_now() -> None:
    atomic = """
    CREATE FUNCTION one() RETURNS integer LANGUAGE sql
    BEGIN ATOMIC
        SELECT 1;
    END;
    """
    with pytest.raises(SqlProofMutationError, match="BEGIN ATOMIC"):
        extract_function(atomic, "one")


def test_build_mutated_ddl_splices_body_and_uses_or_replace() -> None:
    mutated_body = (
        "\n    SELECT COALESCE(SUM(amount), 1) FROM usage_events WHERE user_id = p_user\n"
    )
    ddl = build_mutated_ddl(SCHEMA_SQL, "total_usage", mutated_body)
    assert "OR REPLACE" in ddl.upper()
    assert "COALESCE(SUM(amount), 1)" in ddl
    assert "COALESCE(SUM(amount), 0)" not in ddl


def test_build_mutated_ddl_round_trips_through_pglast() -> None:
    from pglast import parse_sql

    ddl = build_mutated_ddl(SCHEMA_SQL, "total_usage", " SELECT 42 ")
    (raw,) = parse_sql(ddl)
    assert type(raw.stmt).__name__ == "CreateFunctionStmt"
    assert raw.stmt.replace is True
