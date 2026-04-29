from __future__ import annotations

from collections import defaultdict, deque

from sqlproof.exceptions import CircularDependencyError
from sqlproof.schema.model import Table


def insertion_order(tables: tuple[Table, ...]) -> tuple[Table, ...]:
    by_name = {table.name: table for table in tables}
    dependents: dict[str, set[str]] = defaultdict(set)
    indegree = {table.name: 0 for table in tables}

    for table in tables:
        dependencies = {
            fk.referenced_table
            for fk in table.foreign_keys
            if fk.referenced_table != table.name and fk.referenced_table in by_name
        }
        indegree[table.name] = len(dependencies)
        for dependency in dependencies:
            dependents[dependency].add(table.name)

    ready = deque(table.name for table in tables if indegree[table.name] == 0)
    ordered: list[Table] = []
    while ready:
        name = ready.popleft()
        ordered.append(by_name[name])
        for dependent in sorted(dependents[name]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    if len(ordered) != len(tables):
        cycle = ", ".join(sorted(name for name, degree in indegree.items() if degree > 0))
        raise CircularDependencyError(f"Circular foreign-key dependency detected: {cycle}")

    return tuple(ordered)
