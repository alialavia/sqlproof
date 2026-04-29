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
    range_match = re.fullmatch(
        rf"{re.escape(column.name)}\s*(?P<op>>=|>|<=|<)\s*(?P<value>-?\d+(?:\.\d+)?)",
        expression.strip(),
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
