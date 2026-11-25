"""Questions about one class and its project base classes."""

from __future__ import annotations

from typing import NamedTuple

from .project import ClassInfo, MethodInfo


class Member(NamedTuple):
    owner: ClassInfo  # the class in the lineage that declares it
    method: MethodInfo | None  # None for a field


def lineage(cls: ClassInfo) -> list[ClassInfo]:
    """The class, then its project bases breadth first (an approximation of the MRO)."""
    out: list[ClassInfo] = []
    seen: set[str] = set()
    todo = [cls]
    while todo:
        c = todo.pop(0)
        if c.qualname in seen:
            continue
        seen.add(c.qualname)
        out.append(c)
        todo.extend(c.bases)
    return out


def member(cls: ClassInfo, attr: str) -> Member | None:
    """Where ``attr`` is declared: the nearest class with a method or field of that name."""
    for c in lineage(cls):
        if attr in c.methods:
            return Member(c, c.methods[attr])
        if attr in c.fields:
            return Member(c, None)
    return None


def inherits(cls: ClassInfo, ancestor: str) -> bool:
    return any(c.qualname == ancestor for c in lineage(cls))
