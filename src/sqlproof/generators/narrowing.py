"""Narrow a TypeSpec using the column's CHECK constraints.

The knowledge of what `qty >= 0` means belongs with the type knowledge,
not inside strategy construction. Extracting it here lets both the
Hypothesis interpreter (`constraints.py` -> `columns.py`) and the bulk
sampler consume an already-narrowed spec, so neither has to re-derive
what a CHECK implies.

Reading the CHECK text is `sqlproof.schema.checks`'s job: it parses
the expression with pglast (so Postgres's introspected rendering and a
schema file's source text land on the same facts) and splits it into
per-column atoms -- numeric bounds, `length()`/`char_length()` bounds,
allowed-value sets and excluded values. This module folds the atoms
that name `column` into the spec. Conjuncts that aren't one of the
recognised shapes leave the spec unchanged -- narrowing is
best-effort, and Postgres remains the backstop for expressions too
complex to read (`constraints.unhonored_checks` reports them).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any

from sqlproof.generators.typespec import SpecKind, TypeSpec
from sqlproof.schema.checks import analyze_check
from sqlproof.schema.model import CheckConstraint, Column, ParsedCheck

_FIXED_WIDTH_CHAR_TYPES = frozenset({"char", "character", "bpchar"})


def narrow_spec_for_checks(
    spec: TypeSpec,
    column: Column,
    checks: Sequence[CheckConstraint],
) -> TypeSpec:
    """Fold every CHECK on `column` into `spec`.

    Each recognised atom narrows `spec` further (intersecting with
    whatever an earlier atom already narrowed); anything else leaves
    `spec` untouched.
    """
    for atom in column_atoms(column, checks):
        spec = _narrow_atom(spec, column, atom)
    return spec


def column_atoms(column: Column, checks: Sequence[CheckConstraint]) -> list[ParsedCheck]:
    """The recognised CHECK atoms that constrain `column`."""
    return [
        atom
        for check in checks
        for atom in analyze_check(check.expression).atoms
        if atom.column == column.name
    ]


def _narrow_atom(spec: TypeSpec, column: Column, atom: ParsedCheck) -> TypeSpec:
    if atom.kind == "in_set":
        return _narrow_in_set(spec, column, atom.payload)
    if atom.kind == "not_in":
        return _narrow_not_in(spec, atom.payload)
    if atom.kind == "length":
        op, n = atom.payload
        if spec.kind == "text":
            return _narrow_length(spec, op, n)
        if spec.kind == "enum":
            return _filter_enum(spec, lambda v: isinstance(v, str) and _compare(len(v), op, n))
        return spec
    # The only remaining atom kind schema.checks produces is "range".
    op, value = atom.payload
    if spec.kind in {"integer", "decimal"}:
        return _narrow_bound(spec, op, value)
    if spec.kind == "float":
        return _narrow_float_bound(spec, op, value)
    if spec.kind == "enum":
        return _filter_enum(spec, lambda v: _numeric_compare(v, op, value))
    return spec


def _narrow_in_set(spec: TypeSpec, column: Column, raw_values: tuple[Any, ...]) -> TypeSpec:
    if spec.kind == "enum":
        # Already a closed set (a Postgres enum, or an earlier IN-list):
        # intersect rather than replace.
        return _filter_enum(spec, lambda v: any(_same_value(v, w) for w in raw_values))
    values = tuple(_coerce(v, spec.kind) for v in raw_values)
    fixed_width = _underlying_type_name(column) in _FIXED_WIDTH_CHAR_TYPES
    kept = tuple(
        v for v in dict.fromkeys(values) if _admits(spec, v, check_min_size=not fixed_width)
    )
    return TypeSpec(kind="enum", enum_values=kept)


def _narrow_not_in(spec: TypeSpec, raw_values: tuple[Any, ...]) -> TypeSpec:
    if spec.kind == "enum":
        return _filter_enum(spec, lambda v: not any(_same_value(v, w) for w in raw_values))
    values = [_coerce(v, spec.kind) for v in raw_values]
    if spec.kind == "text" and "" in values:
        # `col <> ''` is exactly "at least one character": express it
        # as a size bound so neither interpreter has to reject draws.
        spec = replace(spec, min_size=max(spec.min_size or 0, 1))
        values = [v for v in values if v != ""]
    if not values:
        return spec
    merged = tuple(dict.fromkeys((*spec.excluded_values, *values)))
    return replace(spec, excluded_values=merged)


def _filter_enum(spec: TypeSpec, keep: Callable[[Any], bool]) -> TypeSpec:
    return replace(spec, enum_values=tuple(v for v in spec.enum_values if keep(v)))


def _admits(spec: TypeSpec, value: Any, *, check_min_size: bool) -> bool:
    """Whether a narrowed-to-enum `value` respects bounds already on `spec`."""
    if any(_same_value(value, w) for w in spec.excluded_values):
        return False
    if spec.kind == "text" and isinstance(value, str):
        if spec.max_size is not None and len(value) > spec.max_size:
            return False
        return not (check_min_size and spec.min_size is not None and len(value) < spec.min_size)
    if spec.kind in {"integer", "decimal", "float"}:
        if _as_decimal(value) is None:
            # A literal that isn't a number of this type (only possible
            # in an unvalidated schema file) can't be stored here.
            return False
        if spec.min_value is not None and not _numeric_compare(value, ">=", spec.min_value):
            return False
        return spec.max_value is None or _numeric_compare(value, "<=", spec.max_value)
    return True


def _same_value(a: Any, b: Any) -> bool:
    if a == b:
        return True
    return str(a) == str(b)


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _numeric_compare(value: Any, op: str, bound: Any) -> bool:
    number = _as_decimal(value)
    return number is not None and _compare(number, op, Decimal(str(bound)))


def _compare(left: Any, op: str, right: Any) -> bool:
    if op == ">=":
        return bool(left >= right)
    if op == ">":
        return bool(left > right)
    if op == "<=":
        return bool(left <= right)
    if op == "<":
        return bool(left < right)
    return bool(left == right)


def _underlying_type_name(column: Column) -> str:
    pg_type = column.type
    while pg_type.kind == "domain" and pg_type.base is not None:
        pg_type = pg_type.base
    return pg_type.name.lower()


def _coerce(literal: Any, kind: SpecKind) -> Any:
    # `enum_values` is what a sampler draws from verbatim (no coercion
    # downstream in either interpreter) and gets handed straight to
    # COPY/INSERT, so a value narrowed off an integer or decimal
    # column has to already be the right Python type -- a bare string
    # "1" inserted into an integer column fails there, not here. If a
    # literal doesn't actually parse as the target kind (a malformed
    # or unexpected CHECK), fall back to the raw value rather than
    # raising -- best-effort, matching the rest of this module.
    if isinstance(literal, bool):
        return literal
    if kind == "integer":
        try:
            number = Decimal(str(literal))
        except InvalidOperation:
            return literal
        return int(number) if number == number.to_integral_value() else literal
    if kind == "decimal":
        try:
            return Decimal(str(literal))
        except InvalidOperation:
            return literal
    if kind == "float":
        try:
            return float(literal)
        except (TypeError, ValueError):
            return literal
    if kind == "text" and not isinstance(literal, str):
        return str(literal)
    return literal


def _narrow_float_bound(spec: TypeSpec, op: str, value: Decimal) -> TypeSpec:
    # Postgres compares a float column with a numeric literal in
    # float8 space, so the bound is the literal's float8 value; an
    # exclusive bound steps one float8 ulp past it. Stored as an exact
    # Decimal of that float so the field keeps its declared type.
    bound = float(value)
    if op in (">=", ">"):
        lo = math.nextafter(bound, math.inf) if op == ">" else bound
        current = spec.min_value
        new_lo = Decimal(lo) if current is None else max(Decimal(current), Decimal(lo))
        return replace(spec, min_value=new_lo)
    hi = math.nextafter(bound, -math.inf) if op == "<" else bound
    current = spec.max_value
    new_hi = Decimal(hi) if current is None else min(Decimal(current), Decimal(hi))
    return replace(spec, max_value=new_hi)


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
