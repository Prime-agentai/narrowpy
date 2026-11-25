"""``module-cycle``: sibling modules that import each other in a circle.

Every import counts, wherever it is written. An import moved into a function
or under ``if TYPE_CHECKING:`` stops the ImportError but the modules still
know each other, so the circle is still there for the reader; such imports
are called out as hidden. A package and the modules inside it make no circle:
an edge between two modules is lifted to the two siblings under their
nearest common package, so ``app.a.x -> app.b.y`` is a tie ``app.a -> app.b``.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..findings import HINTS, Finding, Site
from ..graph import a_cycle, sccs
from ..project import Project


@dataclass(frozen=True)
class Tie:
    src: str
    dst: str
    site: Site
    hidden: str  # "", "function-local" or "TYPE_CHECKING"


def related(a: str, b: str) -> bool:
    return a == b or a.startswith(b + ".") or b.startswith(a + ".")


def lift(a: str, b: str) -> tuple[str, str, str] | None:
    if related(a, b):
        return None
    pa, pb = a.split("."), b.split(".")
    k = 0
    while k < min(len(pa), len(pb)) and pa[k] == pb[k]:
        k += 1
    return ".".join(pa[:k]), ".".join(pa[: k + 1]), ".".join(pb[: k + 1])


def import_targets(p: Project, module: str, names: tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    if not names:
        if p.find_module(module) is not None:
            out.add(module)
        else:  # ``import a.b.c`` where only ``a.b`` is ours
            parts = module.split(".")
            for i in range(len(parts) - 1, 0, -1):
                if p.find_module(".".join(parts[:i])) is not None:
                    out.add(".".join(parts[:i]))
                    break
        return out
    for nm in names:
        if nm == "*":
            if p.find_module(module) is not None:
                out.add(module)
            continue
        sym = p.resolve(module, nm)
        if sym is not None:
            out.add(sym.module)
        elif p.find_module(f"{module}.{nm}") is not None:
            out.add(f"{module}.{nm}")
        elif p.find_module(module) is not None:
            out.add(module)
    return out


def ties(p: Project) -> list[Tie]:
    out: list[Tie] = []
    for mod in p.iter_modules():
        for rec in mod.records:
            hidden = "TYPE_CHECKING" if rec.type_only else "function-local" if rec.deferred else ""
            for dst in sorted(import_targets(p, rec.module, rec.names)):
                if dst != mod.name:
                    out.append(Tie(mod.name, dst, Site(mod.src.rel, rec.line), hidden))
    return out


def check(p: Project) -> list[Finding]:
    groups: dict[str, dict[tuple[str, str], list[Tie]]] = {}
    for t in ties(p):
        lifted = lift(t.src, t.dst)
        if lifted is None:
            continue
        parent, a, b = lifted
        groups.setdefault(parent, {}).setdefault((a, b), []).append(t)
    findings: list[Finding] = []
    for parent, pairs in sorted(groups.items()):
        edges: dict[str, set[str]] = {}
        for a, b in pairs:
            edges.setdefault(a, set()).add(b)
        nodes = set(edges) | {b for bs in edges.values() for b in bs}
        for comp in sccs(nodes, edges):
            if len(comp) < 2:
                continue
            members = set(comp)
            inner = {k: v for k, v in pairs.items() if k[0] in members and k[1] in members}
            sites = [t for ts in inner.values() for t in ts]
            hidden = [t for t in sites if t.hidden]
            cheapest = min(inner.items(), key=lambda kv: (len(kv[1]), kv[0]))
            circle = a_cycle(comp[0], members, edges)
            where = f"`{parent}`" if parent else "the top level"
            msg = f"{len(comp)} modules in {where} import each other in a circle: " + ", ".join(f"`{m}`" for m in comp)
            picture = []
            for i, m in enumerate(circle):
                nxt = circle[(i + 1) % len(circle)]
                ts = inner.get((m, nxt), [])
                first = min(ts, key=lambda t: t.site) if ts else None
                tag = f"  [{first.hidden}]" if first is not None and first.hidden else ""
                picture.append(f"{m}  ──imports──▶  {nxt}   ({len(ts)}×, first {first.site if first else '?'}){tag}")
            notes = [f"cheapest tie to cut: `{cheapest[0][0]}` → `{cheapest[0][1]}` "
                     f"({len(cheapest[1])} import{'s' if len(cheapest[1]) != 1 else ''}: "
                     + ", ".join(str(t.site) for t in sorted(cheapest[1], key=lambda t: t.site)[:4]) + ")"]
            runtime_edges: dict[str, set[str]] = {}
            for (a, b), ts in inner.items():
                if any(not t.hidden for t in ts):
                    runtime_edges.setdefault(a, set()).add(b)
            runtime = [c for c in sccs(members, runtime_edges) if len(c) > 1]
            if not runtime:
                notes.append("at import time there is no circle: only the hidden imports close it")
            else:
                biggest = max(runtime, key=len)
                notes.append(f"{sum(len(c) for c in runtime)} of these modules import each other in a circle at import time "
                             f"(largest: " + ", ".join(f"`{m}`" for m in biggest[:5]) + (" ..." if len(biggest) > 5 else "") + ")")
            if hidden:
                notes.append(f"{len(hidden)} of {len(sites)} imports in this circle are hidden "
                             f"(function-local or TYPE_CHECKING): " + ", ".join(str(t.site) for t in sorted(hidden, key=lambda t: t.site)[:5]))
            first_site = min(cheapest[1], key=lambda t: t.site).site
            findings.append(Finding(
                lint="module-cycle",
                key="module-cycle:" + "|".join(comp),
                message=msg,
                site=first_site,
                metric=len(sites),  # import statements in the circle: one more is worse
                notes=notes,
                picture=picture,
                hint=HINTS["module-cycle"],
            ))
    return findings
