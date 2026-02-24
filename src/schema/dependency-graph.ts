import type { TableInfo } from './types.js';

/**
 * Returns table names in valid insertion order (parents before children)
 * using Kahn's topological sort algorithm.
 * Self-referential FKs are excluded from the dependency graph.
 * Throws an Error listing cycle participants if a cycle is detected.
 */
export function getInsertionOrder(tables: TableInfo[]): string[] {
  const names = new Set(tables.map(t => t.name));

  // Build adjacency list: dep[child] = set of parents that must come before it
  const inDegree = new Map<string, number>();
  const dependents = new Map<string, string[]>(); // parent -> list of children

  for (const t of tables) {
    if (!inDegree.has(t.name)) inDegree.set(t.name, 0);
    if (!dependents.has(t.name)) dependents.set(t.name, []);
  }

  for (const t of tables) {
    for (const fk of t.foreignKeys) {
      const parent = fk.referencedTable;
      // Skip self-referential FKs and references to tables not in the schema
      if (parent === t.name || !names.has(parent)) continue;

      inDegree.set(t.name, (inDegree.get(t.name) ?? 0) + 1);
      const children = dependents.get(parent) ?? [];
      children.push(t.name);
      dependents.set(parent, children);
    }
  }

  // Kahn's algorithm
  const queue: string[] = [];
  for (const [name, deg] of inDegree) {
    if (deg === 0) queue.push(name);
  }
  // Sort for deterministic output
  queue.sort();

  const result: string[] = [];
  while (queue.length > 0) {
    const node = queue.shift()!;
    result.push(node);
    const children = dependents.get(node) ?? [];
    for (const child of children) {
      const newDeg = (inDegree.get(child) ?? 1) - 1;
      inDegree.set(child, newDeg);
      if (newDeg === 0) {
        queue.push(child);
        queue.sort();
      }
    }
  }

  if (result.length < tables.length) {
    const remaining = tables
      .map(t => t.name)
      .filter(n => !result.includes(n));
    const cycle = findCycle(remaining, dependents);
    throw new Error(
      `Circular foreign key dependency detected. Involved tables: ${cycle.join(' → ')}`,
    );
  }

  return result;
}

/**
 * DFS to extract a cycle path from the remaining nodes after Kahn's terminates early.
 */
function findCycle(nodes: string[], dependents: Map<string, string[]>): string[] {
  const nodeSet = new Set(nodes);
  const visited = new Set<string>();
  const stack = new Set<string>();
  const path: string[] = [];

  function dfs(node: string): boolean {
    if (stack.has(node)) {
      // Found cycle — extract the cycle portion of path
      const cycleStart = path.indexOf(node);
      return true;
    }
    if (visited.has(node)) return false;

    visited.add(node);
    stack.add(node);
    path.push(node);

    for (const child of dependents.get(node) ?? []) {
      if (!nodeSet.has(child)) continue;
      if (dfs(child)) return true;
    }

    path.pop();
    stack.delete(node);
    return false;
  }

  for (const node of nodes) {
    if (!visited.has(node)) {
      if (dfs(node)) break;
    }
  }

  // Return path with cycle
  if (path.length === 0) return nodes;
  const cycleStart = path[path.length - 1]!;
  const cycleIdx = path.indexOf(cycleStart);
  return cycleIdx >= 0 ? [...path.slice(cycleIdx), cycleStart] : path;
}
