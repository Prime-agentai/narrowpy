"""Strongly connected components and a cycle inside one, without recursion."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping


def sccs(nodes: Iterable[str], edges: Mapping[str, Iterable[str]]) -> list[list[str]]:
    """Tarjan's algorithm, iterative. Components come back with members sorted."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0
    for start in sorted(set(nodes)):
        if start in index:
            continue
        work: list[tuple[str, list[str]]] = [(start, sorted(edges.get(start, ())))]
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            v, succ = work[-1]
            if succ:
                w = succ.pop(0)
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, sorted(edges.get(w, ()))))
                elif w in on_stack:
                    low[v] = min(low[v], index[w])
                continue
            work.pop()
            if work:
                u = work[-1][0]
                low[u] = min(low[u], low[v])
            if low[v] == index[v]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                out.append(sorted(comp))
    return out


def a_cycle(start: str, members: set[str], edges: Mapping[str, Iterable[str]]) -> list[str]:
    """The shortest path from ``start`` back to itself inside ``members``."""
    prev: dict[str, str] = {}
    q: deque[str] = deque([start])
    seen = {start}
    while q:
        v = q.popleft()
        for w in sorted(edges.get(v, ())):
            if w not in members:
                continue
            if w == start:
                path = [v]
                while path[-1] != start:
                    path.append(prev[path[-1]])
                return list(reversed(path))
            if w not in seen:
                seen.add(w)
                prev[w] = v
                q.append(w)
    return [start]
