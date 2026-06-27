from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from sqlproof.schema.model import CheckConstraint, Column


def refine_for_checks(
    column: Column,
    strategy: SearchStrategy[Any],
    checks: tuple[CheckConstraint, ...],
) -> SearchStrategy[Any]:
    for check in checks:
        strategy = _refine_for_check(column, strategy, check.expression)
    return strategy


def _refine_for_check(
    column: Column,
    strategy: SearchStrategy[Any],
    expression: str,
) -> SearchStrategy[Any]:
    expression = _normalize_check_expression(expression)
    in_set_match = re.fullmatch(
        rf"{re.escape(column.name)}\s+IN\s*\((?P<values>.+)\)",
        expression,
        flags=re.IGNORECASE,
    )
    if in_set_match is not None:
        values = tuple(
            _parse_sql_literal(value) for value in in_set_match.group("values").split(",")
        )
        return _sampled_values_for_column(column, values)

    any_array_match = re.fullmatch(
        rf"\(?\s*{re.escape(column.name)}\s*=\s*ANY\s*"
        r"\(\s*ARRAY\[(?P<values>.+)\]\s*\)\s*\)?",
        expression,
        flags=re.IGNORECASE,
    )
    if any_array_match is not None:
        values = tuple(
            _parse_sql_literal(value) for value in any_array_match.group("values").split(",")
        )
        return _sampled_values_for_column(column, values)

    length_match = re.fullmatch(
        rf"(?:char_length|length)\s*\(\s*{re.escape(column.name)}\s*\)\s*"
        rf"(?P<op>>=|>|<=|<|=)\s*(?P<value>\d+)",
        expression,
        flags=re.IGNORECASE,
    )
    if length_match is not None:
        direct = _direct_length_strategy(
            column, length_match.group("op"), int(length_match.group("value"))
        )
        if direct is not None:
            return direct

    range_match = re.fullmatch(
        rf"{re.escape(column.name)}\s*(?P<op>>=|>|<=|<)\s*(?P<value>-?\d+(?:\.\d+)?)",
        expression,
        flags=re.IGNORECASE,
    )
    if range_match is None:
        return strategy
    op = range_match.group("op")
    raw_value = Decimal(range_match.group("value"))
    direct = _direct_range_strategy(column, op, raw_value)
    if direct is not None:
        return direct

    def predicate(value: Any) -> bool:
        if value is None:
            return True
        comparable = Decimal(str(value))
        if op == ">=":
            return comparable >= raw_value
        if op == ">":
            return comparable > raw_value
        if op == "<=":
            return comparable <= raw_value
        return comparable < raw_value

    return strategy.filter(predicate)


def _normalize_check_expression(expression: str) -> str:
    value = expression.strip()
    match = re.fullmatch(r"CHECK\s*\((?P<inner>.*)\)", value, flags=re.IGNORECASE | re.DOTALL)
    if match is not None:
        return match.group("inner").strip()
    return value


def _parse_sql_literal(value: str) -> Any:
    stripped = value.strip()
    cast_match = re.fullmatch(
        r"(?P<literal>'(?:''|[^'])*'|-?\d+(?:\.\d+)?)(?:\s*::[\w. ]+)?",
        stripped,
    )
    if cast_match is not None:
        stripped = cast_match.group("literal")
    if stripped.startswith("'") and stripped.endswith("'"):
        return stripped[1:-1].replace("''", "'")
    try:
        return int(stripped)
    except ValueError:
        try:
            return Decimal(stripped)
        except Exception:
            return stripped


def _sampled_values_for_column(column: Column, values: tuple[Any, ...]) -> SearchStrategy[Any]:
    strategy = st.sampled_from(values)
    if column.nullable:
        return st.none() | strategy
    return strategy


def _underlying_type_name(column: Column) -> str:
    """Resolve a column's effective type name for direct-strategy
    dispatch.

    Domain types alias a base type; for direct integer/numeric
    range refinements we want to look at the base, not the domain
    name. Returns the lowercased name of the underlying type.
    """
    pg_type = column.type
    while pg_type.kind == "domain" and pg_type.base is not None:
        pg_type = pg_type.base
    return pg_type.name.lower()


def _direct_length_strategy(column: Column, op: str, value: int) -> SearchStrategy[str] | None:
    name = _underlying_type_name(column)
    if name not in {"text", "citext", "varchar", "character varying", "char", "character",
                    "bpchar"}:
        return None
    min_size = 0
    max_size = column.type.modifiers[0] if column.type.modifiers else 255
    if op == "=":
        min_size = value
        max_size = value
    elif op == "<=":
        max_size = min(max_size, value)
    elif op == "<":
        max_size = min(max_size, max(0, value - 1))
    elif op == ">=":
        min_size = value
    elif op == ">":
        min_size = value + 1
    if min_size > max_size:
        return st.nothing()
    return st.text(min_size=min_size, max_size=max_size)


def _direct_range_strategy(
    column: Column,
    op: str,
    raw_value: Decimal,
) -> SearchStrategy[Any] | None:
    name = _underlying_type_name(column)
    if op not in {">=", ">"}:
        return None
    minimum = raw_value if op == ">=" else raw_value + Decimal("1")
    if name in {"integer", "int", "int4"}:
        return st.integers(max(int(minimum), -2_147_483_648), 2_147_483_647)
    if name in {"bigint", "int8"}:
        return st.integers(max(int(minimum), -(2**63)), 2**63 - 1)
    if name in {"numeric", "decimal"}:
        modifiers = column.type.modifiers
        if len(modifiers) >= 2:
            precision, scale = modifiers[0], modifiers[1]
            max_abs = Decimal(10) ** (precision - scale) - Decimal(10) ** (-scale)
        else:
            scale = 2
            max_abs = Decimal("1000000")
        return st.decimals(
            min_value=minimum,
            max_value=max_abs,
            places=scale,
            allow_nan=False,
            allow_infinity=False,
        )
    return None


def unique_rows(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> bool:
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        key = tuple(row[column] for column in columns)
        if key in seen:
            return False
        seen.add(key)
    return True
