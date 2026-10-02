"""Interpret CHECK constraint expressions into per-column facts.

Both schema sources hand the generators a CHECK as *text*: live
introspection gets Postgres's own rendering from
``pg_get_constraintdef`` (``CHECK (((char_length(task) >= 1) AND
(char_length(task) <= 200)))`` -- note the extra parentheses, the
``::type`` casts, and ``BETWEEN`` already rewritten to ``>= AND <=``),
while ``from_schema_file`` gets pglast's deparse of what the user
wrote (``char_length(task) BETWEEN 1 AND 200``). Matching either with
regular expressions is brittle, so this module parses the text with
pglast and walks the AST instead, which makes both renderings land on
the same facts.

An expression is split into its top-level ``AND`` conjuncts. Each
conjunct either becomes one or more `ParsedCheck` atoms -- facts the
generators can honor by construction -- or is reported as
*unrecognized*, along with the columns it mentions, so callers can
tell the user that Postgres alone will enforce it.

Recognized conjunct shapes (``col`` is a bare column reference,
``lit`` a literal, optionally ``::cast``, optionally negative):

* ``col <op> lit`` / ``lit <op> col`` for ``>= > <= <`` (numeric
  literals) -> ``range``; ``=`` -> ``in_set``; ``<>`` -> ``not_in``
* ``length(col)`` / ``char_length(col)`` / ``character_length(col)``
  compared with an integer literal (incl. ``=``) -> ``length``
* ``col BETWEEN a AND b`` (and ``BETWEEN SYMMETRIC``), on a column or
  a length call -> two bound atoms
* ``col IN (...)`` and ``col = ANY (ARRAY[...])`` -> ``in_set``;
  ``col NOT IN (...)`` and ``col <> ALL (ARRAY[...])`` -> ``not_in``
* ``<recognized on col> OR col IS NULL`` -> the recognized atoms (the
  generators already admit NULL alongside any narrowed value)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Literal

import pglast
from pglast import ast
from pglast.enums import A_Expr_Kind, BoolExprType, NullTestType
from pglast.keywords import RESERVED_KEYWORDS

from sqlproof.schema.model import ParsedCheck

_CHECK_WRAPPER_RE = re.compile(
    r"^\s*CHECK\s*\((?P<inner>.*)\)\s*(?:NOT\s+VALID|NO\s+INHERIT|\s)*$",
    re.IGNORECASE | re.DOTALL,
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")

_COMPARISON_OPS = frozenset({">=", ">", "<=", "<"})
_FLIPPED = {">=": "<=", ">": "<", "<=": ">=", "<": ">", "=": "=", "<>": "<>"}
_LENGTH_FUNCTIONS = frozenset({"length", "char_length", "character_length"})
_NUMERIC_CASTS = frozenset(
    {
        "int2",
        "int4",
        "int8",
        "smallint",
        "integer",
        "int",
        "bigint",
        "numeric",
        "decimal",
        "float4",
        "float8",
        "real",
        "double precision",
    }
)
# Casts on a column that preserve the comparison's meaning (text-family
# relabels and lossless numeric widenings). A narrowing cast such as
# `(price)::integer` rounds, so a bound on it is not a bound on the
# column and is left unrecognized.
_TRANSPARENT_CASTS = frozenset(
    {
        "text",
        "varchar",
        "character varying",
        "bpchar",
        "citext",
        "numeric",
        "decimal",
        "float8",
        "double precision",
    }
)


def _is(node: Any, cls: type) -> bool:
    # pglast's AST classes are loosely typed (every field is
    # `Unknown | dict | None` to a type checker). Checking node types
    # through this helper instead of a bare `isinstance` keeps nodes
    # typed `Any`, matching how parse_sql.py handles pglast nodes.
    return isinstance(node, cls)


class _NoLiteral:
    """Sentinel: the node is not a literal this module can read."""


_NO_LITERAL = _NoLiteral()


@dataclass(frozen=True, slots=True)
class CheckAnalysis:
    """What one CHECK expression means to the generators.

    `atoms` are the conjuncts that can be honored at generation time;
    `unrecognized` holds, for every conjunct that could not be read,
    the set of column names it references (best effort).
    """

    atoms: tuple[ParsedCheck, ...]
    unrecognized: tuple[frozenset[str], ...]

    @property
    def complete(self) -> bool:
        return not self.unrecognized


def unwrap_check_expression(expression: str) -> str:
    """Strip the ``CHECK (...)`` wrapper Postgres's renderer adds."""
    match = _CHECK_WRAPPER_RE.fullmatch(expression)
    if match is not None:
        return match.group("inner").strip()
    return expression.strip()


@lru_cache(maxsize=4096)
def analyze_check(expression: str) -> CheckAnalysis:
    body = unwrap_check_expression(expression)
    root = _parse_expression(body)
    if root is None:
        # A hand-written expression may use a reserved word as a bare
        # column name (`offset >= 0`); Postgres's own rendering always
        # quotes those, so retry the same way.
        root = _parse_expression(_quote_reserved_identifiers(body))
    if root is None:
        # Unparseable (or not a single expression): we can't say
        # which columns it touches, so fall back to every identifier.
        return CheckAnalysis((), (frozenset(_IDENTIFIER_RE.findall(body)),))

    atoms: list[ParsedCheck] = []
    unrecognized: list[frozenset[str]] = []
    for conjunct in _conjuncts(root):
        recognized = _atoms_for(conjunct)
        if recognized is None:
            unrecognized.append(frozenset(_column_refs(conjunct)))
        else:
            atoms.extend(recognized)
    return CheckAnalysis(tuple(atoms), tuple(unrecognized))


def _parse_expression(body: str) -> Any:
    try:
        statements: Any = pglast.parse_sql(f"SELECT {body}")
    except Exception:
        return None
    if len(statements) != 1:
        return None
    select: Any = statements[0].stmt
    if not _is(select, ast.SelectStmt) or select.targetList is None:
        return None
    if len(select.targetList) != 1 or select.fromClause is not None:
        return None
    return select.targetList[0].val


# Reserved words that legitimately appear inside a CHECK expression;
# every other reserved word in an unparseable expression is taken to
# be a column name.
_EXPRESSION_KEYWORDS = frozenset(
    {
        "all",
        "and",
        "any",
        "array",
        "asymmetric",
        "both",
        "case",
        "cast",
        "collate",
        "current_date",
        "current_time",
        "current_timestamp",
        "current_user",
        "distinct",
        "else",
        "end",
        "false",
        "from",
        "in",
        "leading",
        "localtime",
        "localtimestamp",
        "not",
        "null",
        "or",
        "placing",
        "session_user",
        "some",
        "symmetric",
        "then",
        "trailing",
        "true",
        "user",
        "when",
    }
)
_TOKEN_RE = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|[A-Za-z_][A-Za-z0-9_$]*")


def _quote_reserved_identifiers(body: str) -> str:
    def quote(match: re.Match[str]) -> str:
        token = match.group(0)
        lowered = token.lower()
        if lowered in RESERVED_KEYWORDS and lowered not in _EXPRESSION_KEYWORDS:
            return f'"{token}"'
        return token

    return _TOKEN_RE.sub(quote, body)


def parse_check_expression(expression: str) -> ParsedCheck | None:
    """The `CheckConstraint.parsed` value for `expression`.

    A single honored fact is returned as-is; several are wrapped in a
    ``compound`` ParsedCheck whose payload is the tuple of atoms
    (``column`` is the shared column, or ``""`` when the atoms span
    several). ``None`` means nothing in the expression could be
    interpreted. Conjuncts that could not be interpreted are omitted:
    `parsed` describes what sqlproof honors, not the whole constraint
    -- Postgres stays authoritative for the rest.
    """
    atoms = analyze_check(expression).atoms
    if not atoms:
        return None
    if len(atoms) == 1:
        return atoms[0]
    columns = {atom.column for atom in atoms}
    column = next(iter(columns)) if len(columns) == 1 else ""
    return ParsedCheck(kind="compound", column=column, payload=atoms)


def _conjuncts(node: Any) -> list[Any]:
    if _is(node, ast.BoolExpr) and node.boolop == BoolExprType.AND_EXPR:
        result: list[Any] = []
        for arg in node.args or ():
            result.extend(_conjuncts(arg))
        return result
    return [node]


def _atoms_for(node: Any) -> list[ParsedCheck] | None:
    if _is(node, ast.BoolExpr):
        if node.boolop == BoolExprType.AND_EXPR:
            collected: list[ParsedCheck] = []
            for arg in node.args or ():
                sub = _atoms_for(arg)
                if sub is None:
                    return None
                collected.extend(sub)
            return collected
        if node.boolop == BoolExprType.OR_EXPR:
            return _or_is_null(node)
        return None
    if _is(node, ast.A_Expr):
        return _atoms_for_a_expr(node)
    return None


def _or_is_null(node: Any) -> list[ParsedCheck] | None:
    """``<facts about col> OR col IS NULL`` -> the facts.

    Every generator already treats NULL as admissible next to a
    narrowed value, so the NULL branch adds nothing to honor.
    """
    args = list(node.args or ())
    null_columns = {
        _column_name(arg.arg)
        for arg in args
        if _is(arg, ast.NullTest) and arg.nulltesttype == NullTestType.IS_NULL
    }
    null_columns.discard(None)
    rest = [
        arg
        for arg in args
        if not (_is(arg, ast.NullTest) and arg.nulltesttype == NullTestType.IS_NULL)
    ]
    if len(null_columns) != 1 or len(rest) != 1:
        return None
    atoms = _atoms_for(rest[0])
    if not atoms or any(atom.column not in null_columns for atom in atoms):
        return None
    return atoms


def _atoms_for_a_expr(node: Any) -> list[ParsedCheck] | None:
    op = _operator(node)
    kind = node.kind
    if kind == A_Expr_Kind.AEXPR_OP:
        return _comparison(op, node.lexpr, node.rexpr)
    if kind in (A_Expr_Kind.AEXPR_BETWEEN, A_Expr_Kind.AEXPR_BETWEEN_SYM):
        # pglast always gives BETWEEN a (low, high) pair.
        low_node, high_node = node.rexpr
        low = _numeric(_literal(low_node))
        high = _numeric(_literal(high_node))
        if low is None or high is None:
            return None
        if kind == A_Expr_Kind.AEXPR_BETWEEN_SYM and low > high:
            low, high = high, low
        lower = _bound_atom(node.lexpr, ">=", low)
        upper = _bound_atom(node.lexpr, "<=", high)
        if lower is None or upper is None:
            return None
        return [lower, upper]
    if kind == A_Expr_Kind.AEXPR_IN:
        column = _column_name(node.lexpr)
        values = _literal_list(node.rexpr)
        if column is None or values is None:
            return None
        # IN is `=`, NOT IN is `<>` -- the parser produces no other.
        in_kind: Literal["in_set", "not_in"] = "in_set" if op == "=" else "not_in"
        return [ParsedCheck(kind=in_kind, column=column, payload=values)]
    if kind in (A_Expr_Kind.AEXPR_OP_ANY, A_Expr_Kind.AEXPR_OP_ALL):
        column = _column_name(node.lexpr)
        array = node.rexpr
        if _is(array, ast.TypeCast):
            # varchar columns: `(ARRAY['a'::character varying])::text[]`
            array = array.arg
        if column is None or not _is(array, ast.A_ArrayExpr):
            return None
        values = _literal_list(array.elements)
        if values is None:
            return None
        if kind == A_Expr_Kind.AEXPR_OP_ANY and op == "=":
            return [ParsedCheck(kind="in_set", column=column, payload=values)]
        if kind == A_Expr_Kind.AEXPR_OP_ALL and op == "<>":
            return [ParsedCheck(kind="not_in", column=column, payload=values)]
        return None
    return None


def _comparison(op: str, left: Any, right: Any) -> list[ParsedCheck] | None:
    # The parser already normalizes `!=` to `<>`.
    if op not in _FLIPPED:
        return None
    value = _literal(right)
    subject = left
    if isinstance(value, _NoLiteral):
        # `0 <= col`: read it as `col >= 0`.
        value = _literal(left)
        subject = right
        op = _FLIPPED[op]
    if isinstance(value, _NoLiteral) or value is None:
        return None
    if _length_column(subject) is not None:
        number = _numeric(value)
        if number is None or op not in _COMPARISON_OPS | {"="}:
            return None
        atom = _bound_atom(subject, op, number)
        return None if atom is None else [atom]
    column = _column_name(subject)
    if column is None:
        return None
    if op == "=":
        return [ParsedCheck(kind="in_set", column=column, payload=(value,))]
    if op == "<>":
        return [ParsedCheck(kind="not_in", column=column, payload=(value,))]
    number = _numeric(value)
    if number is None:
        return None
    return [ParsedCheck(kind="range", column=column, payload=(op, number))]


def _bound_atom(subject: Any, op: str, value: Decimal) -> ParsedCheck | None:
    length_column = _length_column(subject)
    if length_column is not None:
        if value != value.to_integral_value():
            return None
        return ParsedCheck(kind="length", column=length_column, payload=(op, int(value)))
    column = _column_name(subject)
    if column is None:
        return None
    return ParsedCheck(kind="range", column=column, payload=(op, value))


def _operator(node: Any) -> str:
    # Every A_Expr carries its operator name as String nodes; a
    # qualified one (`OPERATOR(pg_catalog.>=)`) ends in the bare symbol.
    return str(node.name[-1].sval)


def _column_name(node: Any) -> str | None:
    if _is(node, ast.TypeCast) and _cast_name(node.typeName) in _TRANSPARENT_CASTS:
        # Postgres renders implicit coercions explicitly:
        # `length((code)::text)` for a varchar column,
        # `((status)::text = ANY (...))`, `((qty)::numeric > 0.5)`.
        return _column_name(node.arg)
    if not _is(node, ast.ColumnRef):
        return None
    # A ColumnRef always has fields; the last is the column name, or
    # A_Star for `t.*`, which names no single column.
    last = node.fields[-1]
    return str(last.sval) if _is(last, ast.String) else None


def _length_column(node: Any) -> str | None:
    if not _is(node, ast.FuncCall):
        return None
    names = [n.sval for n in node.funcname or () if _is(n, ast.String)]
    if not names or names[-1].lower() not in _LENGTH_FUNCTIONS:
        return None
    if len(names) > 1 and names[0].lower() != "pg_catalog":
        return None
    args: tuple[Any, ...] = tuple(node.args or ())
    if len(args) != 1:
        return None
    return _column_name(args[0])


def _literal(node: Any) -> Any:
    """Python value of a literal node, or `_NO_LITERAL`."""
    if _is(node, ast.A_Const):
        if node.isnull:
            return None
        val = node.val
        if _is(val, ast.Integer):
            return int(val.ival)
        if _is(val, ast.Float):
            return Decimal(val.fval)
        if _is(val, ast.String):
            return val.sval
        if _is(val, ast.Boolean):
            return bool(val.boolval)
        return _NO_LITERAL
    if _is(node, ast.TypeCast):
        inner = _literal(node.arg)
        if isinstance(inner, str) and _cast_is_numeric(node.typeName):
            try:
                number = Decimal(inner)
            except InvalidOperation:
                return _NO_LITERAL
            if number == number.to_integral_value() and "." not in inner:
                return int(number)
            return number
        return inner
    if _is(node, ast.A_Expr) and node.kind == A_Expr_Kind.AEXPR_OP and node.lexpr is None:
        op = _operator(node)
        inner = _literal(node.rexpr)
        if op in ("-", "+") and isinstance(inner, int | Decimal) and not isinstance(inner, bool):
            return -inner if op == "-" else inner
    return _NO_LITERAL


def _literal_list(nodes: Any) -> tuple[Any, ...] | None:
    # `nodes` is None for an empty `ARRAY[]`.
    if not nodes:
        return None
    values: list[Any] = []
    for node in nodes:
        value = _literal(node)
        if isinstance(value, _NoLiteral) or value is None:
            return None
        values.append(value)
    return tuple(values)


def _cast_name(type_name: Any) -> str | None:
    names = [n.sval for n in getattr(type_name, "names", None) or () if _is(n, ast.String)]
    return names[-1].lower() if names else None


def _cast_is_numeric(type_name: Any) -> bool:
    return _cast_name(type_name) in _NUMERIC_CASTS


def _numeric(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | Decimal):
        return Decimal(value)
    return None


def _column_refs(node: Any) -> set[str]:
    """Every bare column name referenced anywhere under `node`."""
    found: set[str] = set()

    def visit(value: Any) -> None:
        if _is(value, ast.ColumnRef):
            name = _column_name(value)
            if name is not None:
                found.add(name)
            return
        if _is(value, ast.Node):
            for attr in value:
                visit(getattr(value, attr))
        elif _is(value, tuple):
            for item in value:
                visit(item)

    visit(node)
    return found
