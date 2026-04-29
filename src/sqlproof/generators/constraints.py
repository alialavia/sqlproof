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
        return st.sampled_from(values)

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
    if stripped.startswith("'") and stripped.endswith("'"):
        return stripped[1:-1].replace("''", "'")
    try:
        return int(stripped)
    except ValueError:
        try:
            return Decimal(stripped)
        except Exception:
            return stripped


def _direct_length_strategy(column: Column, op: str, value: int) -> SearchStrategy[str] | None:
    name = column.type.name
    if name not in {"text", "citext", "varchar", "character varying", "char", "character"}:
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
    name = column.type.name
    if op not in {">=", ">"}:
        return None
    minimum = raw_value if op == ">=" else raw_value + Decimal("1")
    if name in {"integer", "int", "int4"}:
        return st.integers(max(int(minimum), -2_147_483_648), 2_147_483_647)
    if name in {"bigint", "int8"}:
        return st.integers(max(int(minimum), -(2**63)), 2**63 - 1)
    if name in {"numeric", "decimal"}:
        places = column.type.modifiers[1] if len(column.type.modifiers) > 1 else 2
        return st.decimals(
            min_value=minimum,
            max_value=Decimal("1000000"),
            places=places,
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
