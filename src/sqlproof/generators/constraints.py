"""Turn a column's CHECK constraints into a Hypothesis strategy.

The CHECK text is read by `sqlproof.schema.checks` (postgast, so live
introspection and schema files agree) and folded into the column's
TypeSpec by `narrowing.narrow_spec_for_checks` -- the same narrowing
the bulk path uses -- and the narrowed spec is interpreted by
`columns.strategy_for_spec`. Values are therefore valid by
construction rather than by rejection.

CHECK conjuncts that aren't one of the recognised shapes can't be
honored this way. `unhonored_checks` names the ones that touch columns
sqlproof generates so callers can warn instead of silently producing
rows Postgres will reject.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterable, Sequence
from typing import Any

from hypothesis import strategies as st
from hypothesis.strategies import SearchStrategy

from sqlproof.exceptions import UnhonoredCheckWarning
from sqlproof.generators.columns import strategy_for_spec
from sqlproof.generators.narrowing import (
    column_atoms,
    element_domain_checks,
    narrow_spec_for_checks,
)
from sqlproof.generators.typespec import spec_for_type
from sqlproof.schema.checks import analyze_check
from sqlproof.schema.model import CheckConstraint, Column, Table

__all__ = [
    "UnhonoredCheckWarning",
    "refine_for_checks",
    "unhonored_checks",
    "unique_rows",
    "warn_unhonored_checks",
]


def refine_for_checks(
    column: Column,
    strategy: SearchStrategy[Any],
    checks: tuple[CheckConstraint, ...],
) -> SearchStrategy[Any]:
    """Narrow `strategy` (the column's base strategy) by `checks`.

    Returns `strategy` unchanged when no recognised CHECK atom applies
    to `column`; otherwise a strategy built from the narrowed TypeSpec
    (still admitting NULL for a nullable column).
    """
    if not column_atoms(column, checks) and not element_domain_checks(column.type):
        return strategy
    base = spec_for_type(column.type)
    narrowed = narrow_spec_for_checks(base, column, checks)
    if narrowed == base:
        # The atoms don't apply to this type (e.g. `length()` on an
        # integer column): nothing to narrow.
        return strategy
    refined = strategy_for_spec(narrowed)
    if column.nullable:
        return st.none() | refined
    return refined


def unhonored_checks(
    checks: Iterable[CheckConstraint],
    generated_columns: Iterable[str],
) -> list[str]:
    """CHECK expressions with a conjunct sqlproof can't honor that
    references at least one of `generated_columns`.

    Columns sqlproof doesn't draw values for (defaults, FKs, overrides,
    keys) are excluded: a CHECK only on those can't be violated by
    generation.
    """
    generated = set(generated_columns)
    found: list[str] = []
    for check in checks:
        analysis = analyze_check(check.expression)
        if any(columns & generated for columns in analysis.unrecognized):
            found.append(check.expression)
    return found


def warn_unhonored_checks(
    table: Table,
    checks: Sequence[CheckConstraint],
    generated_columns: Iterable[str],
) -> None:
    for expression in unhonored_checks(checks, generated_columns):
        warnings.warn(
            f"sqlproof cannot interpret CHECK constraint {expression!r} on "
            f"{table.qualified_name}; generated rows may violate it. Pass a "
            "columns={...} override for the affected column(s) to generate "
            "valid values.",
            UnhonoredCheckWarning,
            stacklevel=3,
        )


def warn_reference_checks(
    table: Table,
    referencing_columns: Iterable[str],
    domain_checks: Sequence[CheckConstraint] = (),
) -> None:
    """Warn about CHECKs that restrict foreign-key columns.

    A foreign-key column takes its parent's key values, which sqlproof does
    not filter through the child's own CHECKs. A CHECK such as
    `parent_id > 100` can therefore reject generated rows on load.
    """
    referencing = set(referencing_columns)
    if not referencing:
        return
    for check in (*table.check_constraints, *domain_checks):
        touched = sorted(_check_columns(check.expression) & referencing)
        if not touched:
            continue
        warnings.warn(
            f"CHECK constraint {check.expression!r} on {table.qualified_name} restricts "
            f"foreign-key column(s) {', '.join(touched)}, which take their parent's key "
            "values without that CHECK; generated rows may violate it. Narrow the "
            "parent's key, or pass a columns={...} override for the column.",
            UnhonoredCheckWarning,
            stacklevel=3,
        )


def _check_columns(expression: str) -> set[str]:
    analysis = analyze_check(expression)
    columns: set[str] = set()
    for refs in analysis.unrecognized:
        columns |= refs
    columns.update(atom.column for atom in analysis.atoms if atom.column)
    return columns


def unique_rows(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> bool:
    from sqlproof.generators.rows import _hashable  # pyright: ignore[reportPrivateUsage]

    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        key = tuple(_hashable(row[column]) for column in columns)
        if key in seen:
            return False
        seen.add(key)
    return True
