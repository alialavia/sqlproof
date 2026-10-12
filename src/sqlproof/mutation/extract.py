from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.schema._postgres import node_message, render_statement
from sqlproof.schema._postgres import parse_sql as parse_postgres_sql


@dataclass(frozen=True, slots=True)
class FunctionSource:
    """A CREATE FUNCTION statement located in schema DDL.

    `body` is the AS $$ ... $$ string verbatim; `ddl` is the whole
    statement, deparsed from the AST (so formatting is canonical).
    """

    name: str
    language: str
    body: str
    ddl: str


def _function_statements(schema_sql: str) -> list[Any]:
    try:
        raw_statements: tuple[Any, ...] = tuple(parse_postgres_sql(schema_sql))
    except Exception as exc:
        msg = f"Schema SQL does not parse: {exc}"
        raise SqlProofMutationError(msg) from exc
    return [
        raw.stmt
        for raw in raw_statements
        if type(raw.stmt).__name__ == "CreateFunctionStmt"
    ]


def _matching_statement(schema_sql: str, name: str) -> Any:
    matches = [
        statement
        for statement in _function_statements(schema_sql)
        if statement.funcname[-1].sval == name
    ]
    if not matches:
        msg = f"No CREATE FUNCTION for {name!r} found in the schema SQL."
        raise SqlProofMutationError(msg)
    if len(matches) > 1:
        msg = (
            f"Function {name!r} is ambiguous: {len(matches)} definitions found "
            "(overloads are not supported yet)."
        )
        raise SqlProofMutationError(msg)
    statement = matches[0]
    if statement.sql_body is not None:
        msg = (
            f"Function {name!r} uses a BEGIN ATOMIC body, which is not supported "
            "yet; define it with AS $$ ... $$ instead."
        )
        raise SqlProofMutationError(msg)
    return statement


def _option(statement: Any, defname: str, function_name: str) -> Any:
    for option in statement.options or ():
        if option.defname == defname:
            return option
    msg = f"Function {function_name!r} has no {defname!r} clause."
    raise SqlProofMutationError(msg)


def _body_from_statement(statement: Any, function_name: str) -> str:
    as_option = _option(statement, "as", function_name)
    items: tuple[Any, ...] = tuple(as_option.arg.items)
    if len(items) != 1:
        msg = (
            f"Function {function_name!r} has a multi-part AS clause "
            "(C-language functions are not supported)."
        )
        raise SqlProofMutationError(msg)
    return str(items[0].sval)


def extract_function(schema_sql: str, name: str) -> FunctionSource:
    statement = _matching_statement(schema_sql, name)
    language = str(_option(statement, "language", name).arg.sval)
    body = _body_from_statement(statement, name)
    return FunctionSource(
        name=name, language=language, body=body, ddl=render_statement(statement)
    )


def build_mutated_ddl(schema_sql: str, name: str, mutated_body: str) -> str:
    """CREATE OR REPLACE FUNCTION statement with `mutated_body` spliced in.

    Re-parses the schema and mutates the protobuf AST,
    then deparses — no text splicing, so dollar-quoting and clause order
    are the deparser's problem, not ours.  `_matching_statement` re-parses
    the schema, so the tree mutated here is private to this call.
    """
    statement = _matching_statement(schema_sql, name)
    node_message(statement).replace = True
    as_option = _option(statement, "as", name)
    argument = node_message(as_option).arg.list
    del argument.items[:]
    argument.items.add().string.sval = mutated_body
    return render_statement(statement)
