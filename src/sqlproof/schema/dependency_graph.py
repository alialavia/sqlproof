"""FK dependency-graph resolution.

The job is to take a schema's table set, look at their FK constraints,
and produce an order in which rows can be INSERTed while satisfying
every FK at the moment the row lands.

The hard case is **resolvable cycles**: two or more tables that
reference each other via FKs where at least one FK has nullable
source columns. Concrete shapes that come up in production:

  1. Self-references (parent_id on a hierarchical table). Trivially
     handled by ignoring the self-reference when computing the topo
     order: parent rows are written before child rows within the
     same table.

  2. Current-version-pointer pairs. Example: ``content.current_snapshot_id
     -> snapshots(id)`` (nullable) paired with ``snapshots.content_id
     -> content(id)`` (NOT NULL). The cycle is real but resolvable:
     INSERT content first with ``current_snapshot_id = NULL``, then
     INSERT snapshots (its NOT NULL FK to content is satisfied), then
     UPDATE content.current_snapshot_id.

  3. Nullable mutual-reference pairs. Both FKs are nullable; either
     can be deferred. We pick deterministically (sorted by source
     table name then by columns) so plans are reproducible.

Edges that CAN'T be deferred — every FK in the cycle has at least one
NOT NULL source column — produce a ``CircularDependencyError``. That
case is genuinely unresolvable without ``DEFERRABLE INITIALLY DEFERRED``
constraints, which we don't try to emit.

Invariants the resolver guarantees (see
``tests/unit/test_dependency_graph_cycles.py`` for the property tests
that pin these down):

  (a) Every input table appears exactly once in ``ordered_tables``.
  (b) After removing the deferred edges from the FK graph, each
      table's remaining FK dependencies appear before it in the
      order.
  (c) Every column listed in any ``DeferredEdge.fk_columns`` is
      nullable in its source table. Required because the inserter
      writes those columns as NULL on the initial INSERT.
  (d) Calling the resolver twice on equal input produces equal
      plans (deterministic; relied on by caching/replay layers).
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from sqlproof.exceptions import CircularDependencyError
from sqlproof.schema.model import ForeignKey, Table


@dataclass(frozen=True)
class DeferredEdge:
    """An FK edge that the inserter handles in two passes: write the
    source row with the FK columns as NULL, then UPDATE them after
    the referenced row exists.

    Used only to resolve FK cycles where at least one FK in the cycle
    has all-nullable source columns. See module docstring.

    Attributes are deliberately strings (table/column names), not
    ``Table``/``Column`` references, because downstream consumers
    (core.py's `_insert_dataset`, generators) work with names anyway
    and string-typed edges are easier to serialize for debugging.
    """

    source_table: str
    fk_columns: tuple[str, ...]
    referenced_table: str
    referenced_columns: tuple[str, ...]


@dataclass(frozen=True)
class InsertionPlan:
    """The output of dependency resolution.

    ``ordered_tables`` is the insert order assuming ``deferred_edges``
    have been removed from the FK graph. Consumers that only care
    about the order (and not the deferred-FK-UPDATE pass) can use
    the backward-compatible ``insertion_order()`` wrapper.
    """

    ordered_tables: tuple[Table, ...]
    deferred_edges: tuple[DeferredEdge, ...]


def insertion_order(tables: tuple[Table, ...]) -> tuple[Table, ...]:
    """Return tables in FK-dependency order.

    Backward-compatible wrapper around ``resolve_insertion_plan`` that
    discards the deferred-edge information. Existing callers that
    only need the table order (and use raw INSERTs) keep working.
    Callers that need to write deferred edges via INSERT-then-UPDATE
    should call ``resolve_insertion_plan`` directly.
    """
    return resolve_insertion_plan(tables).ordered_tables


def resolve_insertion_plan(tables: tuple[Table, ...]) -> InsertionPlan:
    """Resolve the FK dependency graph into an executable insertion plan.

    Algorithm:

    1. Build the FK graph, excluding self-references (handled within
       each table) and FKs pointing at tables outside the input set
       (treated as external/satisfied).
    2. Attempt Kahn's topological sort.
    3. If a cycle remains: find an edge in the unprocessed subgraph
       whose source columns are all nullable. Defer it (record in
       ``deferred_edges``, remove from the graph), then retry from
       step 2.
    4. If no deferrable edge exists in any unprocessed cycle, raise
       ``CircularDependencyError``.

    Determinism: when multiple deferrable edges exist, we pick the
    one with the lexicographically smallest ``(source_table,
    fk_columns)`` key. Same algorithmic shape as a stable sort —
    plans are reproducible.

    Complexity: O(V + E) per topological sort pass, O(V) cycle
    iterations in the worst case (each pass removes one edge). So
    O((V + E) * V) overall. For real schemas (V on the order of
    dozens), this is negligible.
    """
    by_name = {table.name: table for table in tables}
    deferred: list[DeferredEdge] = []

    # ``remaining_edges`` is the set of FKs we still need to honor as
    # hard ordering constraints. We mutate this as we defer edges.
    remaining_edges = _initial_edges(tables, by_name)

    while True:
        ordered = _try_topo_sort(tables, remaining_edges, by_name)
        if ordered is not None:
            return InsertionPlan(
                ordered_tables=ordered,
                deferred_edges=tuple(deferred),
            )

        deferrable = _find_deferrable_edge(tables, remaining_edges, by_name)
        if deferrable is None:
            # Every FK in the remaining cycle has at least one NOT NULL
            # column. We can't defer any of them without producing an
            # invalid INSERT. The caller's schema is genuinely
            # cyclic in a way we can't resolve.
            cycle_names = _names_in_remaining_cycle(tables, remaining_edges, by_name)
            raise CircularDependencyError(
                f"Circular foreign-key dependency detected with no "
                f"deferrable edges: {', '.join(cycle_names)}. To resolve, "
                f"make at least one FK in the cycle nullable, or use "
                f"DEFERRABLE INITIALLY DEFERRED constraints in your schema."
            )

        deferred.append(deferrable)
        # Remove the deferred edge from ``remaining_edges`` and retry.
        remaining_edges = tuple(
            edge
            for edge in remaining_edges
            if not (
                edge[0] == deferrable.source_table
                and edge[1].columns == deferrable.fk_columns
            )
        )


def _initial_edges(
    tables: tuple[Table, ...],
    by_name: dict[str, Table],
) -> tuple[tuple[str, ForeignKey], ...]:
    """Build the initial edge list, dropping self-references and FKs
    to tables outside the input set."""
    edges: list[tuple[str, ForeignKey]] = []
    for table in tables:
        for fk in table.foreign_keys:
            if fk.referenced_table == table.name:
                continue
            if fk.referenced_table not in by_name:
                continue
            edges.append((table.name, fk))
    return tuple(edges)


def _try_topo_sort(
    tables: tuple[Table, ...],
    edges: tuple[tuple[str, ForeignKey], ...],
    by_name: dict[str, Table],
) -> tuple[Table, ...] | None:
    """Run Kahn's algorithm. Return the ordered tables if it
    succeeds, ``None`` if a cycle blocks completion."""
    indegree: dict[str, int] = {table.name: 0 for table in tables}
    dependents: dict[str, set[str]] = defaultdict(set)

    for source, fk in edges:
        indegree[source] += 1
        dependents[fk.referenced_table].add(source)

    ready = deque(sorted(name for name in indegree if indegree[name] == 0))
    ordered: list[Table] = []
    while ready:
        name = ready.popleft()
        ordered.append(by_name[name])
        # ``sorted`` keeps the order deterministic across runs.
        for dependent in sorted(dependents[name]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    if len(ordered) != len(tables):
        return None
    return tuple(ordered)


def _find_deferrable_edge(
    tables: tuple[Table, ...],
    edges: tuple[tuple[str, ForeignKey], ...],
    by_name: dict[str, Table],
) -> DeferredEdge | None:
    """Among the edges still in unprocessed SCCs, find one whose
    source columns are all nullable. Return the first such edge in
    sorted order (deterministic pick) or ``None`` if no deferrable
    edge exists."""
    # An edge is "in the cycle" if it's between two tables that didn't
    # make it into the topo sort. Equivalently: both endpoints still
    # have positive in-degree after Kahn drained.
    in_cycle = _tables_in_unprocessed_set(tables, edges)
    if not in_cycle:
        return None

    # Candidates: edges whose source AND referenced table are both
    # in the unprocessed set, AND whose source columns are all nullable.
    candidates: list[tuple[str, ForeignKey]] = []
    for source, fk in edges:
        if source not in in_cycle or fk.referenced_table not in in_cycle:
            continue
        source_table = by_name[source]
        if all(source_table.column(col).nullable for col in fk.columns):
            candidates.append((source, fk))

    if not candidates:
        return None

    # Deterministic pick: lexicographically smallest (source, columns).
    candidates.sort(key=lambda edge: (edge[0], edge[1].columns))
    source, fk = candidates[0]
    return DeferredEdge(
        source_table=source,
        fk_columns=fk.columns,
        referenced_table=fk.referenced_table,
        referenced_columns=fk.referenced_columns,
    )


def _tables_in_unprocessed_set(
    tables: tuple[Table, ...],
    edges: tuple[tuple[str, ForeignKey], ...],
) -> set[str]:
    """Return the set of table names that are in a strongly-connected
    subgraph (i.e. didn't get drained by Kahn's algorithm). Used to
    constrain the deferrable-edge search to actual cycles, ignoring
    edges that would have resolved on their own."""
    indegree: dict[str, int] = {table.name: 0 for table in tables}
    dependents: dict[str, set[str]] = defaultdict(set)
    for source, fk in edges:
        indegree[source] += 1
        dependents[fk.referenced_table].add(source)

    ready = deque(name for name in indegree if indegree[name] == 0)
    processed: set[str] = set()
    while ready:
        name = ready.popleft()
        processed.add(name)
        for dependent in dependents[name]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    return {table.name for table in tables} - processed


def _names_in_remaining_cycle(
    tables: tuple[Table, ...],
    edges: tuple[tuple[str, ForeignKey], ...],
    by_name: dict[str, Table],
) -> list[str]:
    """Sorted list of table names involved in an unresolved cycle.
    Used for the error message when ``CircularDependencyError`` is
    raised."""
    return sorted(_tables_in_unprocessed_set(tables, edges))
