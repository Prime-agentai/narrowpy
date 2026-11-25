"""``tangle``: classes and functions that know each other in a circle.

A circle between things in sibling modules is already a ``module-cycle``, so a
tangle is reported only where that lint cannot see it: when it is bigger than
a reader holds in mind, or when it ties a package to a module inside it
(``app/__init__.py`` and ``app/core.py``), which the module graph treats as
one unit.
"""

from __future__ import annotations

from ..facts import Facts
from ..findings import HINTS, Finding, Site
from ..graph import a_cycle, sccs
from ..project import Project
from .cycles import related


def check(p: Project, facts: Facts, max_size: int) -> list[Finding]:
    edges: dict[str, set[str]] = {}
    first_site: dict[tuple[str, str], Site] = {}
    for r in facts.references:
        if r.thing is None or r.target.kind == "module" or r.target.qualname == r.thing:
            continue
        edges.setdefault(r.thing, set()).add(r.target.qualname)
        key = (r.thing, r.target.qualname)
        if key not in first_site or r.site < first_site[key]:
            first_site[key] = r.site
    nodes = set(edges) | {t for ts in edges.values() for t in ts}
    findings: list[Finding] = []
    for comp in sccs(nodes, edges):
        if len(comp) < 2:
            continue
        modules = sorted({m.rsplit(".", 1)[0] for m in comp})
        nested = any(related(a, b) for i, a in enumerate(modules) for b in modules[i + 1:])
        if not (len(comp) > max_size or (len(modules) > 1 and nested)):
            continue
        members = set(comp)
        circle = a_cycle(comp[0], members, edges)
        picture = []
        for i, m in enumerate(circle):
            nxt = circle[(i + 1) % len(circle)]
            site = first_site.get((m, nxt))
            picture.append(f"{m}  ──uses──▶  {nxt}   ({site})")
        why = (f"{len(comp)} things, more than a reader holds in mind" if len(comp) > max_size
               else "it ties a package to a module inside it")
        shown = ", ".join(f"`{m}`" for m in comp[:max_size]) + (f" and {len(comp) - max_size} more" if len(comp) > max_size else "")
        start = first_site.get((circle[0], circle[1 % len(circle)]))
        findings.append(Finding(
            lint="tangle",
            key="tangle:" + "|".join(comp),
            message=f"{len(comp)} things across {len(modules)} module{'s' if len(modules) != 1 else ''} know each other in a circle: {shown}",
            site=start,
            metric=len(comp),
            notes=[f"why: {why}", "modules: " + ", ".join(f"`{m}`" for m in modules)],
            picture=picture,
            hint=HINTS["tangle"],
        ))
    return findings
