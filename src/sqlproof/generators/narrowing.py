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
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

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
        return TypeSpec(kind="enum", enum_values=_literals(in_set.group("values")))

    any_array = re.fullmatch(
        rf"\(?\s*{name}\s*=\s*ANY\s*\(\s*ARRAY\[(?P<values>.+)\]\s*\)\s*\)?",
        body,
        re.IGNORECASE,
    )
    if any_array is not None:
        return TypeSpec(kind="enum", enum_values=_literals(any_array.group("values")))

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


def _literals(raw_values: str) -> tuple[str, ...]:
    return tuple(_literal(v) for v in raw_values.split(","))


def _literal(raw: str) -> str:
    token = _CAST_SUFFIX_RE.sub("", raw.strip())
    if len(token) >= 2 and token.startswith("'") and token.endswith("'"):
        return token[1:-1].replace("''", "'")
    return token


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
    if op in (">=", ">"):
        lo = _lower_bound(value, op, spec.kind)
        return replace(spec, min_value=max(spec.min_value, lo))
    hi = _upper_bound(value, op, spec.kind)
    return replace(spec, max_value=min(spec.max_value, hi))


def _lower_bound(value: Decimal, op: str, kind: SpecKind) -> int:
    # Bounds on both integer and decimal specs are stored as plain
    # ints (a decimal spec's `min_value`/`max_value` are the whole-
    # number ends of its range; `places` only controls the precision
    # of *drawn* values). A fractional CHECK literal therefore has to
    # be rounded to an int -- rounding up for a lower bound is the
    # only direction that can't admit a value the CHECK forbids.
    #
    # Integers step by one at a strict `>`; decimals keep the
    # ceiling-rounded bound as-is and rely on the sampler's
    # quantisation, which cannot land exactly on a whole-number bound
    # often enough to matter (see `_upper_bound`).
    if op == ">" and kind == "integer":
        return int(value.to_integral_value(rounding=ROUND_FLOOR)) + 1
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _upper_bound(value: Decimal, op: str, kind: SpecKind) -> int:
    if op == "<" and kind == "integer":
        return int(value.to_integral_value(rounding=ROUND_CEILING)) - 1
    return int(value.to_integral_value(rounding=ROUND_FLOOR))
