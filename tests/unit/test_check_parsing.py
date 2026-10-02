"""What `sqlproof.schema.checks` does and does not read out of a CHECK.

The contract that matters: a shape the parser recognizes must produce
exactly the fact Postgres enforces, and anything else must be reported
as *unrecognized* (so the generators warn) rather than misread as a
weaker or different fact.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from sqlproof.schema.checks import analyze_check
from sqlproof.schema.model import ParsedCheck


def _range(column: str, op: str, value: str) -> ParsedCheck:
    return ParsedCheck(kind="range", column=column, payload=(op, Decimal(value)))


def _length(column: str, op: str, value: int) -> ParsedCheck:
    return ParsedCheck(kind="length", column=column, payload=(op, value))


@pytest.mark.parametrize(
    ("expression", "atoms"),
    [
        # Equality against one literal is a one-value set.
        ("x = 5", (ParsedCheck(kind="in_set", column="x", payload=(5,)),)),
        ("flag = true", (ParsedCheck(kind="in_set", column="flag", payload=(True,)),)),
        # Postgres renders a negated cast literal as unary minus on it.
        ("x > - '1'::integer", (_range("x", ">", "-1"),)),
        ("x >= + '2'::integer", (_range("x", ">=", "2"),)),
        # A numeric string cast keeps its fractional part.
        ("x > '1.5'::numeric", (_range("x", ">", "1.5"),)),
        # SYMMETRIC accepts its bounds in either order.
        ("x BETWEEN SYMMETRIC 10 AND 1", (_range("x", ">=", "1"), _range("x", "<=", "10"))),
        # Nested AND inside `... OR col IS NULL` still yields every bound.
        (
            "(n > 0 AND n < 5) OR n IS NULL",
            (_range("n", ">", "0"), _range("n", "<", "5")),
        ),
        # Schema-qualified length function, and its `=` form.
        ("pg_catalog.char_length(code) = 3", (_length("code", "=", 3),)),
        # Unquoted reserved word as a column alongside ordinary columns.
        (
            "offset >= 0 AND limit_value > 1",
            (_range("offset", ">=", "0"), _range("limit_value", ">", "1")),
        ),
    ],
)
def test_recognized_shape_yields_exactly_the_enforced_facts(
    expression: str, atoms: tuple[ParsedCheck, ...]
) -> None:
    analysis = analyze_check(expression)
    assert analysis.complete
    assert analysis.atoms == atoms


@pytest.mark.parametrize(
    ("expression", "columns"),
    [
        # Not a single boolean expression.
        ("", set()),
        ("x > 0; SELECT 1", {"x", "SELECT"}),
        ("a > 0, b > 0", {"a", "b"}),
        ("x > 0 FROM t", {"x", "FROM", "t"}),
        # NOT flips the meaning; reading the inner bound would be wrong.
        ("NOT (x > 0)", {"x"}),
        # Not an operator expression at all.
        ("x IS NOT NULL", {"x"}),
        # OR whose NULL branch is about a different column.
        ("a > 0 OR b IS NULL", {"a", "b"}),
        # The NULL-guarded branch is only partly readable: honoring
        # just `n > 0` would drop the regex on the same column.
        ("(n > 0 AND n ~ '^[0-9]') OR n IS NULL", {"n"}),
        # OR without an IS NULL branch.
        ("a > 0 OR a < -5", {"a"}),
        # BETWEEN on non-numeric bounds, on an expression, NOT BETWEEN,
        # and on a length with a fractional bound.
        ("d BETWEEN '2020-01-01' AND '2021-01-01'", {"d"}),
        ("abs(x) BETWEEN 1 AND 2", {"x"}),
        ("x NOT BETWEEN 1 AND 2", {"x"}),
        ("length(s) BETWEEN 1.5 AND 3", {"s"}),
        # ANY over a non-array, an array with a column in it, an empty
        # array, and a non-equality quantifier.
        ("x = ANY (allowed)", {"x", "allowed"}),
        ("x = ANY (ARRAY[y, 1])", {"x", "y"}),
        ("x = ANY ((ARRAY[])::integer[])", {"x"}),
        ("x > ANY (ARRAY[1, 2])", {"x"}),
        ("x <> ANY (ARRAY[1, 2])", {"x"}),
        # LIKE and other non-comparison operators.
        ("name LIKE 'a%'", {"name"}),
        ("x IS DISTINCT FROM 1", {"x"}),
        # Comparisons the generators can't express as a bound.
        ("length(s) <> 3", {"s"}),
        ("d > '2020-01-01'::date", {"d"}),
        ("x > 'abc'::integer", {"x"}),
        ("x > true", {"x"}),
        ("x <> NULL", {"x"}),
        ("x = B'101'", {"x"}),
        ("x > -y", {"x", "y"}),
        # Function calls that aren't a plain length of a column.
        ("abs(x) > 0", {"x"}),
        ("myschema.length(x) > 2", {"x"}),
        ("length(x, 'UTF8') > 2", {"x"}),
        ("t.* > 0", set()),
        # A narrowing cast: `(price)::integer >= 1` admits price = 0.6.
        ("(price)::integer >= 1", {"price"}),
    ],
)
def test_unsupported_shape_is_reported_unrecognized_not_misread(
    expression: str, columns: set[str]
) -> None:
    analysis = analyze_check(expression)
    assert analysis.atoms == ()
    assert analysis.unrecognized == (frozenset(columns),)
