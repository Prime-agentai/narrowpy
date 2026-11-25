"""``shallow-module``: a module (or package) whose callers must learn too much.

A reader learns an interface one list at a time: the things a module shows,
then the functions of one thing. A module is shallow when the code outside it
uses more functions than a reader holds in mind, at least as many as it keeps
hidden, and one of those lists is too long. A thing is a class (its methods,
plus free functions that take or return it) or a free function on its own.
Pass-through wrappers do no work and are not counted; neither are dunders or
properties. Only callers inside the analysed project count, so a library's
public API, used by its users, does not make it shallow.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass

from ..facts import Facts
from ..findings import HINTS, Finding, Site
from ..project import Project


@dataclass(frozen=True)
class Fn:
    qualname: str  # module.func or module.Class.method
    thing: str  # module.Class, or the function itself


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _working_functions(p: Project, modules: set[str]) -> dict[str, Fn]:
    out: dict[str, Fn] = {}
    here = [m for m in p.iter_modules() if m.name in modules]
    classes_here = {c.qualname for m in here for c in m.classes}
    for c in (c for m in here for c in m.classes):
        for m in c.methods.values():
            if m.kind == "property" or _is_dunder(m.name) or m.wrapper:
                continue
            q = f"{c.qualname}.{m.name}"
            out[q] = Fn(q, c.qualname)
    for f in (f for m in here for f in m.functions):
        if f.wrapper or _is_dunder(f.name):
            continue
        thing = f.qualname
        mod = f.mod
        anns = [a.annotation for a in f.node.args.posonlyargs + f.node.args.args + f.node.args.kwonlyargs] + [f.node.returns]
        for ann in anns:
            if ann is None:
                continue
            for n in ast.walk(ann):
                if isinstance(n, (ast.Name, ast.Attribute)):
                    sym = p.resolve_expr(mod, n)
                    if sym is not None and sym.qualname in classes_here:
                        thing = sym.qualname
                        break
            if thing != f.qualname:
                break
        out[f.qualname] = Fn(f.qualname, thing)
    return out


def _home(p: Project, qualname: str) -> str:
    """The module a working function (``mod.func`` or ``mod.Class.method``) is written in."""
    fn = p.find_function(qualname)
    return fn.module if fn is not None else qualname.rsplit(".", 2)[0]


def _unit(p: Project, facts: Facts, name: str, modules: set[str], max_size: int) -> tuple[Finding, set[str]] | None:
    fns = _working_functions(p, modules)
    if len(fns) <= max_size:
        return None
    shown: dict[str, Site] = {}
    for r in facts.references:
        if r.module in modules or r.target.kind != "function":
            continue
        if r.target.qualname in fns:
            shown.setdefault(r.target.qualname, r.site)
    for u in facts.method_uses:
        if u.module in modules:
            continue
        q = f"{u.owner}.{u.method}"
        if q in fns:
            shown.setdefault(q, u.site)
    hidden = len(fns) - len(shown)
    if len(shown) <= max_size or len(shown) < hidden:
        return None
    lists: dict[str, list[str]] = defaultdict(list)
    for q in shown:
        lists[fns[q].thing].append(q)
    long_lists = {t: qs for t, qs in lists.items() if len(qs) > max_size}
    if len(lists) <= max_size and not long_lists:
        return None
    picture = [f"shows {len(shown):>3}  {'█' * min(len(shown), 40)}", f"hides {hidden:>3}  {'░' * min(hidden, 40)}"]
    if len(lists) > max_size:
        picture.append(f"callers learn {len(lists)} things (more than {max_size})")
    for t, qs in sorted(long_lists.items(), key=lambda kv: -len(kv[1])):
        picture.append(f"`{t.rsplit('.', 1)[-1]}`: {len(qs)} functions used outside (more than {max_size})")
    notes = []
    for t, qs in sorted(lists.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:max_size]:
        names = ", ".join(sorted(q.rsplit(".", 1)[-1] for q in qs)[:max_size])
        more = f" +{len(qs) - max_size}" if len(qs) > max_size else ""
        notes.append(f"`{t.rsplit('.', 1)[-1]}`: {names}{more}")
    callers = sorted({r.module for r in facts.references if r.module not in modules and r.target.qualname in shown}
                     | {u.module for u in facts.method_uses if u.module not in modules and f"{u.owner}.{u.method}" in shown})
    if callers:
        notes.append("used from: " + ", ".join(f"`{c}`" for c in callers[:max_size]) + (" ..." if len(callers) > max_size else ""))
    mod = p.find_module(name)
    site = Site(mod.src.rel, 1) if mod else min(shown.values())
    kind = "package" if mod is None or mod.src.is_package else "module"
    return Finding(
        lint="shallow-module",
        key=f"shallow-module:{name}",
        message=f"{kind} `{name}` shows {len(shown)} functions and hides {hidden}",
        site=site,
        metric=len(shown),
        notes=notes,
        picture=picture,
        hint=HINTS["shallow-module"],
    ), set(shown)


def check(p: Project, facts: Facts, max_size: int) -> list[Finding]:
    findings: list[Finding] = []
    flagged: set[str] = set()
    for name in [m.name for m in p.iter_modules()]:
        hit = _unit(p, facts, name, {name}, max_size)
        if hit is not None:
            findings.append(hit[0])
            flagged.add(name)
    names = [m.name for m in p.iter_modules()]
    for pkg in [m.name for m in p.iter_modules() if m.src.is_package]:
        subtree = {m for m in names if m == pkg or m.startswith(pkg + ".")}
        if len(subtree) < 2:
            continue
        hit = _unit(p, facts, pkg, subtree, max_size)
        if hit is None:
            continue
        finding, shown = hit
        inside = flagged & subtree
        # Already said: every function the package shows comes from one module we reported.
        if len(inside) == 1 and all(_home(p, q) in inside for q in shown):
            continue
        findings.append(finding)
    return findings
