"""The PostgreSQL parser operations shared by schema and mutation code."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import postgast
from postgast import pg_query_pb2 as pg
from postgast.nodes import AstNode, wrap


def parse_sql(sql: str) -> tuple[Any, ...]:
    tree: Any = wrap(postgast.parse(sql))
    return tuple(tree.stmts)


def node_message(node: AstNode) -> Any:
    # postgast's typed wrappers expose read-only properties. Keep access to
    # their underlying protobuf here, for rendering and function-body edits.
    return node._pb  # pyright: ignore[reportPrivateUsage]


def _copy_node(target: Any, node: AstNode) -> None:
    message = node_message(node)
    field = next(
        field
        for field in pg.Node.DESCRIPTOR.fields
        if field.message_type is not None and field.message_type.name == message.DESCRIPTOR.name
    )
    getattr(target, field.name).CopyFrom(message)


def render_statement(node: AstNode) -> str:
    tree = pg.ParseResult()
    _copy_node(tree.stmts.add().stmt, node)
    return str(postgast.deparse(tree))


def render_expression(node: AstNode) -> str:
    # libpg_query deparses complete statements. A SELECT wrapper lets it
    # render defaults and CHECK expressions without a second SQL renderer.
    tree = postgast.parse("SELECT NULL")
    _copy_node(tree.stmts[0].stmt.select_stmt.target_list[0].res_target.val, node)
    return str(postgast.deparse(tree))[len("SELECT ") :]


@lru_cache(maxsize=256)
def is_reserved_keyword(word: str) -> bool:
    tokens = postgast.scan(word).tokens
    return len(tokens) == 1 and tokens[0].keyword_kind == pg.RESERVED_KEYWORD
