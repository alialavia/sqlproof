from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pglast import parse_sql as parse_postgres_sql

# parse_plpgsql_json returns the raw JSON string; we re-dump with sort_keys for a
# deterministic AST key (pglast.parse_plpgsql is just json.loads of this).
from pglast.parser import parse_plpgsql_json

from sqlproof.exceptions import SqlProofMutationError
from sqlproof.mutation.extract import (
    FunctionSource,
    build_mutated_ddl,
    extract_function,
)
from sqlproof.mutation.model import Mutant, MutationSet, Op, Replace


@dataclass(frozen=True, slots=True)
class PreparedMutant:
    """A validated mutant, ready to run: no further parsing can fail."""

    mutant: Mutant
    mutant_id: str
    ddl: str


def apply_op(body: str, op: Op) -> str:
    if isinstance(op, Replace):
        pattern, replacement = op.old, op.new
    else:
        pattern, replacement = op.pattern, ""
    count = body.count(pattern)
    if count == 0:
        msg = f"{op.describe()}: pattern not found in the function body."
        raise SqlProofMutationError(msg)
    if count > 1:
        msg = (
            f"{op.describe()}: pattern occurs {count} times; "
            "extend it until it is unique."
        )
        raise SqlProofMutationError(msg)
    return body.replace(pattern, replacement, 1)


def _sql_ast_key(sql: str, *, context: str) -> str:
    try:
        # Key on the statement nodes, not RawStmt wrappers, so that
        # stmt_location/stmt_len (which encode formatting) don't affect identity.
        return repr(tuple(raw.stmt for raw in parse_postgres_sql(sql)))
    except Exception as exc:
        msg = f"{context}: body does not parse — authoring error: {exc}"
        raise SqlProofMutationError(msg) from exc


def _strip_linenos(node: object) -> object:
    """Recursively remove 'lineno' keys from a parsed plpgsql JSON structure."""
    if isinstance(node, dict):
        return {k: _strip_linenos(v) for k, v in node.items() if k != "lineno"}
    if isinstance(node, list):
        return [_strip_linenos(item) for item in node]
    return node


def _plpgsql_ast_key(ddl: str, *, context: str) -> str:
    try:
        raw: Any = parse_plpgsql_json(ddl)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        return json.dumps(_strip_linenos(parsed), sort_keys=True)
    except Exception as exc:
        msg = f"{context}: body does not parse — authoring error: {exc}"
        raise SqlProofMutationError(msg) from exc


def _ast_keys(
    source: FunctionSource,
    mutated_body: str,
    mutated_ddl: str,
    *,
    context: str,
) -> tuple[str, str]:
    """Return (original_key, mutated_key) for no-op detection and identity.

    For SQL functions, keys are derived from the body text alone.
    For PL/pgSQL, keys are derived from the full DDL (body parsing requires
    the full CREATE FUNCTION wrapper).
    For unknown languages, whitespace-normalized text is used as a fallback.
    """
    if source.language == "sql":
        return (
            _sql_ast_key(source.body, context=f"{context} (original)"),
            _sql_ast_key(mutated_body, context=context),
        )
    if source.language == "plpgsql":
        return (
            _plpgsql_ast_key(source.ddl, context=f"{context} (original)"),
            _plpgsql_ast_key(mutated_ddl, context=context),
        )
    # Unknown language: no parser available; fall back to
    # whitespace-normalized text so no-op detection still works.
    return (" ".join(source.body.split()), " ".join(mutated_body.split()))


def prepare_mutants(mutations: MutationSet, schema_sql: str) -> tuple[PreparedMutant, ...]:
    """Validate every mutant eagerly — all authoring errors surface before
    any database is touched."""
    prepared: list[PreparedMutant] = []
    seen: dict[str, str] = {}
    for mutant in mutations.mutants:
        context = mutant.describe()
        source = extract_function(schema_sql, mutant.target_name)
        body = source.body
        for op in mutant.ops:
            try:
                body = apply_op(body, op)
            except SqlProofMutationError as exc:
                msg = f"{mutant.target_name}: {exc}"
                raise SqlProofMutationError(msg) from None
        ddl = build_mutated_ddl(schema_sql, mutant.target_name, body)
        original_key, mutated_key = _ast_keys(source, body, ddl, context=context)
        if original_key == mutated_key:
            msg = f"{context}: mutation is a no-op (identical AST after parsing)."
            raise SqlProofMutationError(msg)
        digest = hashlib.sha256(
            f"{mutant.target_name}\n{mutated_key}".encode()
        ).hexdigest()[:16]
        if digest in seen:
            msg = (
                f"{context}: duplicate of mutant {seen[digest]!r} "
                "(both produce the same mutated AST)."
            )
            raise SqlProofMutationError(msg)
        seen[digest] = context
        prepared.append(PreparedMutant(mutant=mutant, mutant_id=digest, ddl=ddl))
    return tuple(prepared)
