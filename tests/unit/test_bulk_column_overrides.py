"""Per-column overrides on the bulk generation path.

The Hypothesis path has taken `columns={...}` overrides for a long time and
`AGENTS.md` teaches them. The bulk path had no equivalent, so its ranges were
unreachable magic numbers -- a real gap for a generator whose whole job is
producing data realistic enough that planner statistics mean something.

These tests pin three things: that bulk uses the same `"table.column"` keying,
that it resolves overrides at the same point in the per-column branch order as
`rows.py` (a mismatch there means the same `columns={...}` behaves differently
depending on which generator ran), and the one place the two interfaces
deliberately differ.
"""

from __future__ import annotations

import random

import pytest
from hypothesis import strategies as st

from sqlproof.exceptions import SqlProofUsageError
from sqlproof.generators.bulk import bulk_table_rows
from sqlproof.generators.rows import _unique_value
from sqlproof.schema.parse_sql import parse_schema_sql

SCHEMA = """
CREATE TABLE items (
  id bigint PRIMARY KEY,
  label text NOT NULL,
  tier text NOT NULL DEFAULT 'free',
  qty integer NOT NULL CHECK (qty >= 0)
);
"""


def _rows(count: int, **kwargs: object) -> list[dict[str, object]]:
    table = parse_schema_sql(SCHEMA).table("items")
    return list(
        bulk_table_rows(
            table, count=count, rng=random.Random(1), parent_counts={}, **kwargs
        )
    )


def test_plain_value_override_is_used_verbatim() -> None:
    rows = _rows(20, columns={"items.label": "fixed-label"})
    assert [r["label"] for r in rows] == ["fixed-label"] * 20


def test_callable_override_receives_the_row_being_built() -> None:
    seen: list[tuple[str, int, dict[str, object]]] = []

    def label_for(context: object) -> str:
        seen.append(
            (context.column_name, context.row_index, dict(context.row))  # type: ignore[attr-defined]
        )
        return f"item-{context.row_index}"  # type: ignore[attr-defined]

    rows = _rows(3, columns={"items.label": label_for})
    assert [r["label"] for r in rows] == ["item-0", "item-1", "item-2"]
    assert [name for name, _, _ in seen] == ["label"] * 3
    # The partially-built row is visible: `id` is assigned before `label`.
    assert all("id" in partial for _, _, partial in seen)


def test_callable_context_has_no_cross_row_history() -> None:
    """Bulk streams, so it cannot offer `table_rows` / `rows_by_table` the way
    the Hypothesis path's ColumnContext does -- holding every prior row is
    exactly the memory cost this path exists to avoid. A callable written
    against the Hypothesis context must fail loudly here rather than silently
    see an empty list and generate subtly different data."""

    def wants_history(context: object) -> int:
        return len(context.table_rows)  # type: ignore[attr-defined]

    with pytest.raises(AttributeError):
        _rows(2, columns={"items.label": wants_history})


def test_search_strategy_override_raises_rather_than_being_ignored() -> None:
    """A Hypothesis strategy is the natural thing to reach for here, and
    drawing from one would pull the conjecture machinery -- whose per-draw cost
    grows with example size -- back into the path that exists to avoid it.
    Refusing loudly beats silently ignoring it."""
    with pytest.raises(SqlProofUsageError, match="SearchStrategy"):
        _rows(2, columns={"items.label": st.text()})


def test_override_reaches_a_column_that_has_a_default() -> None:
    """`rows.py` checks the override BEFORE the default skip, so a defaulted
    column is overridable there. The bulk loop must agree."""
    rows = _rows(5, columns={"items.tier": "enterprise"})
    assert [r["tier"] for r in rows] == ["enterprise"] * 5


def test_default_column_is_still_omitted_without_an_override() -> None:
    rows = _rows(3)
    assert all("tier" not in r for r in rows)


def test_primary_key_override_is_ignored_matching_rows_py() -> None:
    """`rows.py` assigns the single-column PK before it looks at overrides, so
    an override on the PK never applies. Deterministic keys are what let FKs be
    satisfied by arithmetic, so honouring an override here would silently break
    every child table."""
    rows = _rows(4, columns={"items.id": 999})
    assert [r["id"] for r in rows] == [_unique_value("id", "bigint", i) for i in range(4)]


def test_override_bypasses_check_narrowing() -> None:
    """Matching the Hypothesis path: the user's value wins outright. Postgres
    still rejects it at load time if it genuinely violates the constraint."""
    rows = _rows(3, columns={"items.qty": -5})
    assert [r["qty"] for r in rows] == [-5] * 3


def test_unrelated_override_key_leaves_generation_alone() -> None:
    rows = _rows(5, columns={"other_table.label": "ignored"})
    assert all(r["label"] != "ignored" for r in rows)
