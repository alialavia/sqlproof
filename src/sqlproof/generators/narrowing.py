"""Narrow a TypeSpec using the column's CHECK constraints.

The knowledge of what `qty >= 0` means belongs with the type knowledge,
not inside strategy construction. Extracting it here lets both the
Hypothesis interpreter and the bulk sampler consume an already-narrowed
spec, so neither has to re-derive what a CHECK implies.

Recognises the same four shapes `constraints.py`'s `_refine_for_check`
does today (IN-list, `= ANY(ARRAY[...])`, `length()`/`char_length()`,
and a single comparison against a column). Anything else returns the
spec unchanged -- narrowing is best-effort, and Postgres remains the
backstop for expressions too complex to read.

This module intentionally does not import from `constraints.py` (nor
the reverse, yet): a later task retargets `constraints.py` to call
`narrow_spec_for_checks` and build its strategy from the result, which
would make an import in the other direction circular.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from sqlproof.generators.typespec import SpecKind, TypeSpec
from sqlproof.schema.model import CheckConstraint, Column

# Postgres's `pg_get_constraintdef` renders a table-level CHECK as the
# literal text `CHECK (<expr>)`. Domain CHECKs go through the same
# renderer. Strip that wrapper before matching, mirroring
# `constraints.py`'s `_normalize_check_expression` -- otherwise none of
# the shape regexes below would ever match a live-introspected check.
_CHECK_WRAPPER_RE = re.compile(r"CHECK\s*\((?P<inner>.*)\)", re.IGNORECASE | re.DOTALL)

# A `::type` cast suffix on an IN-list / ANY(ARRAY[...]) literal, e.g.
# `'free'::text`.
_CAST_SUFFIX_RE = re.compile(r"\s*::[\w. ]+$")


def narrow_spec_for_checks(
    spec: TypeSpec,
    column: Column,
    checks: Sequence[CheckConstraint],
) -> TypeSpec:
    """Fold every CHECK on `column` into `spec`, narrowest-first order.

    Each check either narrows `spec` further (intersecting with
    whatever an earlier check already narrowed) or, if its expression
    isn't one of the recognised shapes, leaves `spec` untouched.
    """
    for check in checks:
        spec = _narrow_one(spec, column, check.expression)
    return spec


def _narrow_one(spec: TypeSpec, column: Column, expression: str) -> TypeSpec:
    body = _normalize_check_expression(expression)
    name = re.escape(column.name)

    in_set = re.fullmatch(rf"{name}\s+IN\s*\((?P<values>.+)\)", body, re.IGNORECASE)
    if in_set is not None:
        values = _literals(in_set.group("values"), spec.kind)
        return TypeSpec(kind="enum", enum_values=values)

    any_array = re.fullmatch(
        rf"\(?\s*{name}\s*=\s*ANY\s*\(\s*ARRAY\[(?P<values>.+)\]\s*\)\s*\)?",
        body,
        re.IGNORECASE,
    )
    if any_array is not None:
        values = _literals(any_array.group("values"), spec.kind)
        return TypeSpec(kind="enum", enum_values=values)

    length = re.fullmatch(
        rf"(?:char_length|length)\s*\(\s*{name}\s*\)\s*"
        r"(?P<op>>=|>|<=|<|=)\s*(?P<value>\d+)",
        body,
        re.IGNORECASE,
    )
    if length is not None:
        if spec.kind != "text":
            return spec
        return _narrow_length(spec, length.group("op"), int(length.group("value")))

    bound = re.fullmatch(
        rf"{name}\s*(?P<op>>=|>|<=|<)\s*(?P<value>-?\d+(?:\.\d+)?)", body, re.IGNORECASE
    )
    if bound is None or spec.kind not in {"integer", "decimal"}:
        return spec
    return _narrow_bound(spec, bound.group("op"), Decimal(bound.group("value")))


def _normalize_check_expression(expression: str) -> str:
    value = expression.strip()
    match = _CHECK_WRAPPER_RE.fullmatch(value)
    if match is not None:
        return match.group("inner").strip()
    return value


def _literals(raw_values: str, kind: SpecKind) -> tuple[Any, ...]:
    return tuple(_coerce(_literal(v), kind) for v in raw_values.split(","))


def _literal(raw: str) -> str:
    token = _CAST_SUFFIX_RE.sub("", raw.strip())
    if len(token) >= 2 and token.startswith("'") and token.endswith("'"):
        return token[1:-1].replace("''", "'")
    return token


def _coerce(literal: str, kind: SpecKind) -> Any:
    # `enum_values` is what a sampler draws from verbatim (no coercion
    # downstream in either interpreter) and gets handed straight to
    # COPY/INSERT, so a value narrowed off an integer or decimal
    # column has to already be the right Python type -- a bare string
    # "1" inserted into an integer column fails there, not here. A
    # genuine Postgres enum's labels (spec.kind == "enum" before this
    # narrowing runs) are untouched, since those are legitimately
    # strings. If a literal doesn't actually parse as the target kind
    # (a malformed or unexpected CHECK), fall back to the raw string
    # rather than raising -- best-effort, matching the rest of this
    # module.
    if kind == "integer":
        try:
            return int(literal)
        except ValueError:
            return literal
    if kind == "decimal":
        try:
            return Decimal(literal)
        except InvalidOperation:
            return literal
    return literal


def _narrow_length(spec: TypeSpec, op: str, n: int) -> TypeSpec:
    # `lo`/`hi` of None means "this comparison doesn't touch that
    # side" (e.g. `<=` never constrains the lower bound) -- in that
    # case the intersection below must keep whatever bound was
    # already there rather than widen it.
    lo, hi = {
        "=": (n, n),
        "<=": (None, n),
        "<": (None, max(n - 1, 0)),
        ">=": (n, None),
        ">": (n + 1, None),
    }[op]
    min_size = _tighten(spec.min_size, lo, max)
    max_size = _tighten(spec.max_size, hi, min)
    return replace(spec, min_size=min_size, max_size=max_size)


def _tighten(
    existing: int | None,
    candidate: int | None,
    combine: Callable[[int, int], int],
) -> int | None:
    if candidate is None:
        return existing
    if existing is None:
        return candidate
    return combine(existing, candidate)


def _narrow_bound(spec: TypeSpec, op: str, value: Decimal) -> TypeSpec:
    assert spec.min_value is not None and spec.max_value is not None
    if spec.kind == "integer":
        return _narrow_integer_bound(spec, op, value)
    return _narrow_decimal_bound(spec, op, value)


def _narrow_integer_bound(spec: TypeSpec, op: str, value: Decimal) -> TypeSpec:
    # An integer spec's bounds are, and must stay, plain `int` --
    # there's no representable value between two consecutive integers,
    # so an exclusive bound steps by a whole 1.
    assert spec.min_value is not None and spec.max_value is not None
    if op in (">=", ">"):
        int_lo = _int_lower_bound(value, op)
        return replace(spec, min_value=max(int(spec.min_value), int_lo))
    int_hi = _int_upper_bound(value, op)
    return replace(spec, max_value=min(int(spec.max_value), int_hi))


def _narrow_decimal_bound(spec: TypeSpec, op: str, value: Decimal) -> TypeSpec:
    # Unlike an integer spec, a decimal spec's `min_value`/`max_value`
    # can hold a `Decimal` (see typespec.py), so a non-strict bound
    # needs no rounding at all -- it's just the literal. A strict
    # bound steps by one unit in the last place at the spec's own
    # scale (`Decimal("1.0001")` at scale 4 for `rate > 1`), the
    # smallest step that can't admit the literal itself. A whole-1
    # step (the integer treatment) would over-narrow every strict
    # decimal bound for no reason, and composing two of them close
    # together (e.g. `1 < rate < 2`) would invert min/max into an
    # empty range that nothing downstream checks for.
    assert spec.min_value is not None and spec.max_value is not None
    if op in (">=", ">"):
        dec_lo = value if op == ">=" else value + _decimal_ulp(spec.places)
        return replace(spec, min_value=max(Decimal(spec.min_value), dec_lo))
    dec_hi = value if op == "<=" else value - _decimal_ulp(spec.places)
    return replace(spec, max_value=min(Decimal(spec.max_value), dec_hi))


def _int_lower_bound(value: Decimal, op: str) -> int:
    if op == ">":
        return int(value.to_integral_value(rounding=ROUND_FLOOR)) + 1
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _int_upper_bound(value: Decimal, op: str) -> int:
    if op == "<":
        return int(value.to_integral_value(rounding=ROUND_CEILING)) - 1
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _decimal_ulp(places: int | None) -> Decimal:
    # One unit in the last place at the spec's declared scale -- the
    # smallest step that moves a decimal bound strictly past the
    # literal without overshooting it. `places` is normally set (the
    # `numeric` builder in typespec.py defaults it to 2), but a
    # defensively-constructed spec could carry no scale at all; with
    # no unit to step by, fall back to the old whole-1 step for that
    # case only -- over-narrow rather than guess a precision.
    if places is None:
        return Decimal(1)
    return Decimal(1).scaleb(-places)
