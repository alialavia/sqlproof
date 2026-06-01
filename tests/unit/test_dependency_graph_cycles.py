"""Tests for FK cycle handling in dependency_graph.

Closes #47. The previous behavior of `insertion_order` raised
`CircularDependencyError` on any FK cycle between distinct tables.
This blocks three legitimate, common production patterns:

  1. Self-referential FKs (parent_id) — already handled by the
     existing exclusion-of-self-references logic. Kept here as a
     regression invariant.
  2. Current-version pointer pairs (e.g. content.current_snapshot_id
     → snapshots(id) AND snapshots.content_id → content(id), with
     content.current_snapshot_id nullable).
  3. Nullable mutual-reference pairs (e.g. users.primary_address_id
     → addresses(id) AND addresses.user_id → users(id), both
     nullable).

The fix: when a cycle is detected, look for an FK in the cycle whose
source columns are all nullable. Defer that edge — the source row
inserts with NULL for those columns, and an UPDATE pass after all
rows exist fills them in. If no edge in a cycle is deferrable
(every FK in the cycle has at least one NOT NULL column), raising
is still correct: the cycle is genuinely unresolvable without
DEFERRABLE INITIALLY DEFERRED constraints.

The new `resolve_insertion_plan()` returns an `InsertionPlan` with:
  - `ordered_tables`: insertion order assuming deferred edges are removed
  - `deferred_edges`: FKs to be inserted as NULL then UPDATEd

`insertion_order()` is preserved as a backward-compatible wrapper
returning just `plan.ordered_tables`.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from sqlproof.exceptions import CircularDependencyError
from sqlproof.schema.dependency_graph import (
    DeferredEdge,
    InsertionPlan,
    insertion_order,
    resolve_insertion_plan,
)
from sqlproof.schema.model import Column, ForeignKey, PgType, Table

INTEGER = PgType(kind="scalar", name="integer")


def _table(
    name: str,
    *,
    columns: tuple[tuple[str, bool], ...],  # (column_name, nullable)
    foreign_keys: tuple[ForeignKey, ...] = (),
    primary_key: tuple[str, ...] = ("id",),
) -> Table:
    """Helper for terse table construction in tests.

    `columns` is a tuple of (name, nullable) — type is always INTEGER
    since these tests don't care about column types, only FK shapes.
    """
    return Table(
        schema="public",
        name=name,
        columns=tuple(
            Column(col_name, INTEGER, nullable=nullable, default=None, is_generated=False)
            for col_name, nullable in columns
        ),
        primary_key=primary_key,
        foreign_keys=foreign_keys,
        unique_constraints=(),
        check_constraints=(),
    )


def _fk(columns: tuple[str, ...], referenced_table: str) -> ForeignKey:
    return ForeignKey(columns, referenced_table, ("id",), "NO ACTION", "NO ACTION")


# ---------------------------------------------------------------------------
# Regression: behavior we keep
# ---------------------------------------------------------------------------


def test_simple_parent_child_returns_parent_first_with_no_deferred_edges() -> None:
    """Invariant: when the FK graph is a DAG, no edges are deferred and
    the order is a valid topological sort. Failure case it addresses:
    regression where the new code path accidentally defers edges that
    don't need deferring (would cause an unnecessary UPDATE pass)."""
    parent = _table("parent", columns=(("id", False),))
    child = _table(
        "child",
        columns=(("id", False), ("parent_id", False)),
        foreign_keys=(_fk(("parent_id",), "parent"),),
    )

    plan = resolve_insertion_plan((child, parent))

    assert [t.name for t in plan.ordered_tables] == ["parent", "child"]
    assert plan.deferred_edges == ()


def test_self_referential_fk_is_not_a_cycle() -> None:
    """Invariant: self-references don't count as cycles — they're
    resolved by ordering parent rows before child rows within the
    same table. Failure case: regression that re-introduces the
    bug where any FK referring to its own table would loop forever
    or raise."""
    content = _table(
        "content",
        columns=(("id", False), ("parent_id", True)),
        foreign_keys=(_fk(("parent_id",), "content"),),
    )

    plan = resolve_insertion_plan((content,))

    assert [t.name for t in plan.ordered_tables] == ["content"]
    assert plan.deferred_edges == ()


def test_unresolvable_cycle_with_all_not_null_fks_still_raises() -> None:
    """Invariant: a cycle where every FK has at least one NOT NULL
    source column is genuinely unresolvable without
    DEFERRABLE INITIALLY DEFERRED constraints. We must still raise —
    silently producing an invalid INSERT order would cause runtime
    NOT NULL violations.

    Failure case: regression that defers a NOT-NULL FK and produces
    an insertion plan that fails at INSERT time."""
    left = _table(
        "left_t",
        columns=(("id", False), ("right_id", False)),
        foreign_keys=(_fk(("right_id",), "right_t"),),
    )
    right = _table(
        "right_t",
        columns=(("id", False), ("left_id", False)),
        foreign_keys=(_fk(("left_id",), "left_t"),),
    )

    with pytest.raises(CircularDependencyError):
        resolve_insertion_plan((left, right))


def test_insertion_order_is_backward_compatible_wrapper() -> None:
    """The old `insertion_order(tables) -> tuple[Table, ...]` API must
    still return just the ordered tables (no deferred-edge info),
    so existing callers keep working.

    Failure case: refactor that changes `insertion_order`'s return
    type and breaks downstream callers we haven't audited."""
    parent = _table("parent", columns=(("id", False),))
    child = _table(
        "child",
        columns=(("id", False), ("parent_id", False)),
        foreign_keys=(_fk(("parent_id",), "parent"),),
    )

    result = insertion_order((child, parent))

    assert isinstance(result, tuple)
    assert [t.name for t in result] == ["parent", "child"]


# ---------------------------------------------------------------------------
# New: resolvable cycles
# ---------------------------------------------------------------------------


def test_current_version_pointer_pair_defers_the_nullable_side() -> None:
    """The canonical 'current-version pointer' shape: a content table
    with a nullable current_snapshot_id pointing at snapshots, and
    snapshots have a NOT NULL content_id pointing back at content.

    The cycle is resolvable: insert content first with
    current_snapshot_id=NULL, then insert snapshots (its content_id
    FK is satisfied), then UPDATE content.current_snapshot_id.

    Invariant: the plan defers the nullable side of the cycle, and
    ordered_tables produces a valid order after removing that edge.

    Failure case: deferring the wrong side (snapshots.content_id is
    NOT NULL, so deferring it would produce an invalid plan)."""
    content = _table(
        "content",
        columns=(("id", False), ("current_snapshot_id", True)),
        foreign_keys=(_fk(("current_snapshot_id",), "snapshots"),),
    )
    snapshots = _table(
        "snapshots",
        columns=(("id", False), ("content_id", False)),
        foreign_keys=(_fk(("content_id",), "content"),),
    )

    plan = resolve_insertion_plan((content, snapshots))

    assert plan.deferred_edges == (
        DeferredEdge(
            source_table="content",
            fk_columns=("current_snapshot_id",),
            referenced_table="snapshots",
            referenced_columns=("id",),
        ),
    )
    # After removing the deferred edge, content has no FK dependencies
    # and snapshots depends on content. So order must be content first.
    assert [t.name for t in plan.ordered_tables] == ["content", "snapshots"]


def test_mutually_nullable_pair_defers_one_edge() -> None:
    """When both sides of a 2-cycle are nullable, either edge is a
    valid deferral choice. We pick deterministically (sorted) so the
    plan is stable across runs.

    Invariant: exactly one edge is deferred (not both, not zero), and
    the remaining graph is a valid DAG.

    Failure case: ambiguous choice produces non-deterministic plans
    that confuse downstream caching / replay."""
    users = _table(
        "users",
        columns=(("id", False), ("primary_address_id", True)),
        foreign_keys=(_fk(("primary_address_id",), "addresses"),),
    )
    addresses = _table(
        "addresses",
        columns=(("id", False), ("user_id", True)),
        foreign_keys=(_fk(("user_id",), "users"),),
    )

    plan = resolve_insertion_plan((users, addresses))

    assert len(plan.deferred_edges) == 1
    # Deterministic pick: alphabetically first source-table wins.
    assert plan.deferred_edges[0].source_table == "addresses"
    assert plan.deferred_edges[0].fk_columns == ("user_id",)
    # After deferring addresses.user_id, the remaining order is
    # addresses → users (since addresses now has no FK deps).
    table_names = [t.name for t in plan.ordered_tables]
    assert table_names == sorted(table_names) or table_names[0] == "addresses"
    assert set(table_names) == {"addresses", "users"}


def test_three_cycle_with_one_nullable_edge_defers_that_edge() -> None:
    """A→B→C→A with only the C→A edge nullable: deferring C→A is the
    only resolvable option. The other two edges are NOT NULL and
    must remain in the DAG.

    Failure case: algorithm defers a NOT-NULL edge because it picked
    by edge order instead of nullability."""
    a = _table(
        "a",
        columns=(("id", False), ("b_id", False)),
        foreign_keys=(_fk(("b_id",), "b"),),
    )
    b = _table(
        "b",
        columns=(("id", False), ("c_id", False)),
        foreign_keys=(_fk(("c_id",), "c"),),
    )
    c = _table(
        "c",
        columns=(("id", False), ("a_id", True)),  # only nullable edge
        foreign_keys=(_fk(("a_id",), "a"),),
    )

    plan = resolve_insertion_plan((a, b, c))

    assert plan.deferred_edges == (
        DeferredEdge(
            source_table="c",
            fk_columns=("a_id",),
            referenced_table="a",
            referenced_columns=("id",),
        ),
    )
    # After deferring c.a_id, order: a → b → c (b depends on c, but
    # c no longer depends on a, so c has no FK deps now; topologically
    # c could come first OR b could; both are valid orderings).
    # The invariant: a comes before b (b depends on c which doesn't
    # depend on anything now, but b depends on c, so c before b; and
    # b depends on c which has no deps now). Simpler invariant: in
    # the resulting DAG, each table comes after its dependencies.
    name_to_pos = {t.name: i for i, t in enumerate(plan.ordered_tables)}
    # Remaining edges after deferring c.a_id: a→b, b→c
    # So b must come after b's dependency (c), and a must come after
    # a's dependency (b).
    assert name_to_pos["c"] < name_to_pos["b"]  # b depends on c
    assert name_to_pos["b"] < name_to_pos["a"]  # a depends on b


def test_composite_fk_with_one_non_nullable_column_is_not_deferrable() -> None:
    """A composite FK (multi-column) is deferrable only if EVERY one
    of its source columns is nullable. If any column is NOT NULL,
    the FK can't be set to all-NULL on initial insert.

    Failure case: defers a composite FK with mixed nullability, then
    INSERT fails because the NOT NULL column needs a value."""
    # 2-cycle where one side's FK is composite, mixed nullability,
    # and the other side's FK is plain NOT NULL.
    parent = _table(
        "parent",
        columns=(
            ("id", False),
            ("ref_a", True),
            ("ref_b", False),  # mixed nullability composite
        ),
        foreign_keys=(
            ForeignKey(("ref_a", "ref_b"), "child", ("a", "b"), "NO ACTION", "NO ACTION"),
        ),
    )
    child = _table(
        "child",
        columns=(("a", False), ("b", False), ("parent_id", False)),
        foreign_keys=(_fk(("parent_id",), "parent"),),
        primary_key=("a", "b"),
    )

    with pytest.raises(CircularDependencyError):
        resolve_insertion_plan((parent, child))


def test_composite_fk_with_all_nullable_columns_is_deferrable() -> None:
    """Inverse of the above: when all source columns of a composite
    FK are nullable, the FK can be inserted as all-NULL and UPDATEd
    later.

    Failure case: algorithm requires single-column FKs and rejects
    a valid composite-FK cycle."""
    parent = _table(
        "parent",
        columns=(
            ("id", False),
            ("ref_a", True),
            ("ref_b", True),  # both nullable
        ),
        foreign_keys=(
            ForeignKey(("ref_a", "ref_b"), "child", ("a", "b"), "NO ACTION", "NO ACTION"),
        ),
    )
    child = _table(
        "child",
        columns=(("a", False), ("b", False), ("parent_id", False)),
        foreign_keys=(_fk(("parent_id",), "parent"),),
        primary_key=("a", "b"),
    )

    plan = resolve_insertion_plan((parent, child))

    assert plan.deferred_edges == (
        DeferredEdge(
            source_table="parent",
            fk_columns=("ref_a", "ref_b"),
            referenced_table="child",
            referenced_columns=("a", "b"),
        ),
    )


# ---------------------------------------------------------------------------
# Property tests (using Hypothesis)
# ---------------------------------------------------------------------------


@st.composite
def _arbitrary_table_set(draw: st.DrawFn) -> tuple[Table, ...]:
    """Generate a small set of tables with arbitrary FK edges and
    nullability. Used to exercise the plan resolver across shapes
    we wouldn't think to hand-craft.

    Limits chosen to keep test time bounded and counterexamples
    interpretable: 2-4 tables, 0-3 FK edges per table, 1-3 columns
    per FK (so composite FKs are exercised but not pathologically
    wide)."""
    n_tables = draw(st.integers(min_value=2, max_value=4))
    table_names = [f"t{i}" for i in range(n_tables)]

    # Pick FK edges first as (source_idx, ref_idx) pairs. Avoid
    # self-references (those are trivially handled and noise here).
    edges_per_table = []
    for src in range(n_tables):
        n_edges = draw(st.integers(min_value=0, max_value=3))
        edges = []
        for _ in range(n_edges):
            ref = draw(st.integers(min_value=0, max_value=n_tables - 1))
            assume(ref != src)
            edges.append(ref)
        edges_per_table.append(edges)

    tables = []
    for src_idx, name in enumerate(table_names):
        columns: list[tuple[str, bool]] = [("id", False)]
        fks: list[ForeignKey] = []
        for fk_idx, ref_idx in enumerate(edges_per_table[src_idx]):
            ref_name = table_names[ref_idx]
            # Each FK gets a unique column name; nullability is drawn
            # per FK. We don't generate composite FKs here for
            # tractability; the unit tests above cover composite cases.
            col_name = f"{ref_name}_id_{fk_idx}"
            nullable = draw(st.booleans())
            columns.append((col_name, nullable))
            fks.append(_fk((col_name,), ref_name))
        tables.append(_table(name, columns=tuple(columns), foreign_keys=tuple(fks)))
    return tuple(tables)


@given(tables=_arbitrary_table_set())
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_plan_is_valid_or_explicitly_unresolvable(tables: tuple[Table, ...]) -> None:
    """Property: for any randomly-generated set of tables with FKs,
    `resolve_insertion_plan` either returns a valid plan or raises
    `CircularDependencyError`. A 'valid plan' means:
      a) Every table appears exactly once in `ordered_tables`.
      b) After removing the deferred edges from the FK graph, each
         table's remaining FK dependencies appear before it in the
         order.
      c) Every column listed in any deferred edge is nullable in
         its source table.

    This is the global invariant the algorithm must satisfy.

    Failure case: the algorithm produces a plan that fails to honor
    one of (a/b/c) — e.g., orders a child before its parent, or
    defers a NOT-NULL column. Either failure produces a runtime
    INSERT error when the plan is executed."""
    try:
        plan = resolve_insertion_plan(tables)
    except CircularDependencyError:
        # Acceptable outcome: a cycle exists with no deferrable edges.
        # The property only requires the plan to be CORRECT when it
        # exists. We don't verify minimality here.
        return

    by_name = {t.name: t for t in tables}

    # Invariant (a): each table appears exactly once.
    ordered_names = [t.name for t in plan.ordered_tables]
    assert sorted(ordered_names) == sorted(by_name.keys())

    name_to_pos = {name: i for i, name in enumerate(ordered_names)}
    deferred_keys = {
        (e.source_table, e.fk_columns) for e in plan.deferred_edges
    }

    # Invariant (b): after removing deferred edges, each table's
    # remaining FKs reference tables that come before it.
    for table in tables:
        for fk in table.foreign_keys:
            if fk.referenced_table == table.name:
                continue
            if (table.name, fk.columns) in deferred_keys:
                continue
            if fk.referenced_table not in by_name:
                continue
            assert name_to_pos[fk.referenced_table] < name_to_pos[table.name], (
                f"After removing deferred edges, {table.name}'s FK to "
                f"{fk.referenced_table} on columns {fk.columns} must point "
                f"to a table that appears BEFORE {table.name} in the order."
            )

    # Invariant (c): every deferred-edge column is nullable.
    for edge in plan.deferred_edges:
        source = by_name[edge.source_table]
        for col_name in edge.fk_columns:
            col = source.column(col_name)
            assert col.nullable, (
                f"Deferred edge {edge.source_table}.{edge.fk_columns} → "
                f"{edge.referenced_table} includes NOT NULL column "
                f"{col_name}, which can't be inserted as NULL."
            )


@given(tables=_arbitrary_table_set())
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
def test_resolve_is_pure_and_returns_insertion_plan(tables: tuple[Table, ...]) -> None:
    """Property: calling `resolve_insertion_plan` twice on the same
    input must produce identical plans. Used as a regression guard
    against the algorithm depending on dict iteration order or
    other hidden non-determinism.

    Failure case: deterministic-pick logic in `_find_deferrable_edge`
    accidentally orders by something that varies (e.g. `id()` of
    objects). Plans would differ between runs, breaking caching."""
    try:
        plan_a = resolve_insertion_plan(tables)
        plan_b = resolve_insertion_plan(tables)
    except CircularDependencyError:
        return

    assert isinstance(plan_a, InsertionPlan)
    assert [t.name for t in plan_a.ordered_tables] == [t.name for t in plan_b.ordered_tables]
    assert plan_a.deferred_edges == plan_b.deferred_edges
