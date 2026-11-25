"""``field-leak``: code outside a class's module that works on the class's fields.

A class guards its fields when it has behaviour of its own: a regular method
that uses ``self``'s fields. Records (dataclasses, NamedTuples, enums,
exceptions and plain classes without such a method) hold data for others and
guard nothing. A guarding class that lets every field out is a data structure
with helpers, and is left alone too, unless a field is private.

Outsiders are code in another module that is not the class or a subclass. In
Python the module is the unit of privacy, so helper functions next to the
class are insiders. Working on a field means writing it, indexing it, calling
a method on it, or reaching through it to its attributes. Reading a value is
no leak, and neither is any use of a field that holds a plain value (str, int,
Path, ...). A field whose name starts with ``_`` is private by its author's
word: any use from outside is a leak.
"""

from __future__ import annotations

import ast
from collections import defaultdict

from ..facts import Access, Facts, is_private
from ..findings import HELD_IN_MIND, HINTS, Finding
from ..hierarchy import inherits, lineage
from ..project import ClassInfo, Project
from ..types import ClassTy, Typer

RECORD_BASES = {
    "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "NamedTuple", "TypedDict", "Protocol",
    "Exception", "BaseException", "ValueError", "TypeError", "RuntimeError", "KeyError", "OSError",
}
#: ``reach`` (reading an attribute of a field) is a read: it is recorded, not reported.
LEAK_KINDS = {"write", "index", "call"}


def is_record(cls: ClassInfo) -> bool:
    chain = lineage(cls)
    for c in chain:
        if set(c.external_bases) & RECORD_BASES or any(b.endswith(("Error", "Exception")) for b in c.external_bases):
            return True
    fields = {f for c in chain for f in c.fields}
    for c in chain:
        for m in c.methods.values():
            if m.kind != "method" or (m.name.startswith("__") and m.name.endswith("__")):
                continue
            if not m.node.args.args:
                continue
            me = m.node.args.args[0].arg
            for n in ast.walk(m.node):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == me and n.attr in fields:
                    return False
    return True


def _holds_value(typer: Typer, owner: ClassInfo, fld: str) -> bool:
    """A plain value, or an instance of an immutable project class: using it only reads it."""
    if typer.field_is_value(owner, fld):
        return True
    t = typer.field_type(owner.qualname, fld)
    return isinstance(t, ClassTy) and typer.is_immutable(t.q)


def is_outsider(p: Project, owner: ClassInfo, a: Access) -> bool:
    if a.module == owner.module:
        return False
    inside = p.find_class(a.from_class) if a.from_class is not None else None
    if inside is not None and inherits(inside, owner.qualname):
        return False
    return True


def check(p: Project, facts: Facts, typer: Typer) -> list[Finding]:
    by_field: dict[tuple[str, str], list[Access]] = defaultdict(list)
    for a in facts.accesses:
        owner = p.find_class(a.owner)
        if owner is None or not is_outsider(p, owner, a):
            continue
        private = is_private(a.field)
        kind = a.kind
        if kind in {"index", "call"} and not private and _holds_value(typer, owner, a.field):
            kind = "read"
        if private or kind in LEAK_KINDS:
            by_field[(a.owner, a.field)].append(a)
    findings: list[Finding] = []
    by_class: dict[str, set[str]] = defaultdict(set)
    for owner_q, fld in by_field:
        by_class[owner_q].add(fld)
    for owner_q, leaked in sorted(by_class.items()):
        owner = p.find_class(owner_q)
        if owner is None:
            continue
        record = is_record(owner)
        kept = sorted(f for f in owner.fields if f not in leaked)
        shares_all = not kept and not any(is_private(f) for f in owner.fields)
        for fld in sorted(leaked):
            if not is_private(fld) and (record or shares_all):
                continue
            sites = sorted(by_field[(owner_q, fld)], key=lambda a: a.site)
            per_module: dict[str, list[Access]] = defaultdict(list)
            for a in sites:
                per_module[a.module].append(a)
            n, k = len(sites), len(per_module)
            cls_name = owner.name
            msg = (f"`{cls_name}.{fld}` is known outside `{owner.module}`: "
                   f"{n} place{'s' if n != 1 else ''} in {k} module{'s' if k != 1 else ''}")
            if is_private(fld):
                msg += " (private field)"
            notes = []
            for a in sites[:HELD_IN_MIND]:
                where = a.thing or a.module
                notes.append(f"{a.site}: {a.kind} in `{where}`" + ("  (receiver inferred from the unique field name)" if a.via == "name" else ""))
            if n > HELD_IN_MIND:
                notes.append(f"... and {n - HELD_IN_MIND} more")
            others = sorted(f for f in leaked if f != fld)
            mods = sorted(per_module.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            picture = [f"{owner.qualname}  ({owner.mod.src.rel}:{owner.line})"]
            for i, (m, acc) in enumerate(mods[:HELD_IN_MIND]):
                kinds = ", ".join(sorted({x.kind for x in acc}))
                lead = f"  {fld} ◀── " if i == 0 else "  " + " " * len(fld) + " ◀── "
                picture.append(f"{lead}{len(acc)}  {m}  ({kinds})")
            if others:
                picture.append(f"  also known outside: {', '.join(others[:HELD_IN_MIND])}")
            if kept:
                picture.append(f"  kept: {', '.join(kept[:HELD_IN_MIND])}")
            first = sites[0].site
            findings.append(Finding(
                lint="field-leak",
                key=f"field-leak:{owner_q}.{fld}",
                message=msg,
                site=first,
                metric=n,
                notes=notes,
                picture=picture,
                hint=HINTS["field-leak"],
            ))
    return findings
