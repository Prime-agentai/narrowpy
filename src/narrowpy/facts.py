"""One walk over every scope, recording who knows what.

* ``Reference``: code in a thing (a top-level class or function) names another
  project class or function.
* ``Access``: code works on a field of a project class, and how: ``read``,
  ``write``, ``index``, ``call`` (a method called on the field) or ``reach``
  (an attribute of the field).
* ``MethodUse``: code calls or names a method of a project class.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Literal

from .findings import Site
from .project import ClassInfo, Module, Project, Symbol
from .types import ClassTy, Scope, Typer, module_nodes, own_nodes

AccessKind = Literal["read", "write", "index", "call", "reach"]


@dataclass(frozen=True)
class Reference:
    thing: str | None  # qualname of the class/function the code is in; None at module level
    module: str
    target: Symbol
    site: Site


@dataclass(frozen=True)
class Access:
    owner: str  # class that declares the field
    field: str
    kind: AccessKind
    module: str
    from_class: str | None
    thing: str | None
    site: Site
    via: Literal["type", "name"]


@dataclass(frozen=True)
class MethodUse:
    owner: str
    method: str
    module: str
    thing: str | None
    site: Site


@dataclass
class Facts:
    references: list[Reference] = field(default_factory=list)
    accesses: list[Access] = field(default_factory=list)
    method_uses: list[MethodUse] = field(default_factory=list)


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def is_private(name: str) -> bool:
    return name.startswith("_") and not _is_dunder(name)


class Collector:
    def __init__(self, project: Project) -> None:
        self.p = project
        self.typer = Typer(project)
        self.facts = Facts()
        self._private_owner: dict[str, list[str]] = {}
        for c in project.classes.values():
            for f in c.fields:
                if is_private(f):
                    self._private_owner.setdefault(f, []).append(c.qualname)

    def run(self) -> Facts:
        for mod in self.p.modules.values():
            self._module(mod)
        return self.facts

    # -- units ----------------------------------------------------------------------

    def _module(self, mod: Module) -> None:
        mscope = self.typer.module_scope(mod)
        self._unit(mod, mod.tree, mscope, None, module_level=True)
        for stmt in mod.tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{mod.name}.{stmt.name}"
                self._function(mod, stmt, None, None, q)
            elif isinstance(stmt, ast.ClassDef):
                q = f"{mod.name}.{stmt.name}"
                if q in self.p.classes:
                    self._class(mod, self.p.classes[q])

    def _class(self, mod: Module, cls: ClassInfo) -> None:
        body_scope = Scope(mod, {}, cls.qualname, {}, self.typer.module_scope(mod))
        self._unit(mod, cls.node, body_scope, cls.qualname, module_level=False)
        for stmt in cls.node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if stmt.name in cls.methods and cls.methods[stmt.name].node is stmt:
                    scope = self.typer.method_scope(cls, stmt.name)
                else:
                    scope = self.typer.function_scope(mod, stmt, cls.qualname, None)
                self._unit(mod, stmt, scope, cls.qualname, module_level=False)
                self._nested(mod, stmt, scope, cls.qualname)

    def _function(self, mod: Module, fn: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
                  self_cls: str | None, outer: Scope | None, thing: str) -> None:
        scope = self.typer.function_scope(mod, fn, self_cls, outer)
        self._unit(mod, fn, scope, thing, module_level=False)
        self._nested(mod, fn, scope, thing)

    def _nested(self, mod: Module, root: ast.AST, scope: Scope, thing: str) -> None:
        for n in own_nodes(root):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                self._function(mod, n, scope.self_cls, scope, thing)
            elif isinstance(n, ast.ClassDef):
                inner = Scope(mod, {}, None, {}, scope)
                self._unit(mod, n, inner, thing, module_level=False)
                for stmt in n.body:
                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self._function(mod, stmt, None, scope, thing)

    # -- one scope ------------------------------------------------------------------

    def _unit(self, mod: Module, root: ast.AST, scope: Scope, thing: str | None, module_level: bool) -> None:
        nodes = own_nodes(root) if not module_level else module_nodes(mod.tree)
        parent: dict[ast.AST, ast.AST] = {}
        for n in [root, *nodes]:
            for c in ast.iter_child_nodes(n):
                parent[c] = n
        consumed: set[int] = set()
        imports = scope.imports()
        # The first positional parameter of a method is its own instance.
        me: str | None = None
        if isinstance(root, (ast.FunctionDef, ast.AsyncFunctionDef)) and scope.self_cls and root.args.args:
            me = root.args.args[0].arg
        for n in nodes:
            if id(n) in consumed:
                continue
            if isinstance(n, ast.Attribute):
                sym = self.p.resolve_expr(mod, n, local=imports) if not self._is_local_chain(scope, n) else None
                if sym is not None:
                    self._mark_chain(n, consumed)
                    if sym.kind != "module":
                        self._ref(thing, mod, sym, n)
                    continue
                self._member(mod, scope, n, parent, thing, me)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                if self._is_local(scope, n.id):
                    continue
                sym = self.p.resolve_expr(mod, n, local=imports)
                if sym is not None and sym.kind != "module":
                    self._ref(thing, mod, sym, n)

    def _ref(self, thing: str | None, mod: Module, sym: Symbol, n: ast.AST) -> None:
        if thing is not None and sym.qualname == thing:
            return
        self.facts.references.append(Reference(thing, mod.name, sym, Site(mod.src.rel, getattr(n, "lineno", 0))))

    @staticmethod
    def _mark_chain(n: ast.expr, consumed: set[int]) -> None:
        while isinstance(n, ast.Attribute):
            consumed.add(id(n.value))
            n = n.value

    @staticmethod
    def _is_local(scope: Scope, name: str) -> bool:
        s: Scope | None = scope
        while s is not None and s.outer is not None:  # the module scope is not local
            if name in s.env:
                return True
            s = s.outer
        return False

    def _is_local_chain(self, scope: Scope, n: ast.Attribute) -> bool:
        base: ast.expr = n
        while isinstance(base, ast.Attribute):
            base = base.value
        return isinstance(base, ast.Name) and self._is_local(scope, base.id)

    def _member(self, mod: Module, scope: Scope, n: ast.Attribute, parent: dict[ast.AST, ast.AST],
                thing: str | None, me: str | None) -> None:
        site = Site(mod.src.rel, n.lineno)
        cls_sym = self.p.resolve_expr(mod, n.value, local=scope.imports()) if not self._is_local_chain_expr(scope, n.value) else None
        if cls_sym is not None and cls_sym.kind == "class":
            m = self.p.method_of(cls_sym.qualname, n.attr)
            if m is not None:
                self.facts.method_uses.append(MethodUse(m[0].qualname, n.attr, mod.name, thing, site))
            return
        recv = self.typer.expr(scope, n.value)
        if isinstance(recv, ClassTy):
            owner = self.p.field_owner(recv.q, n.attr)
            if owner is not None:
                self.facts.accesses.append(Access(owner.qualname, n.attr, _kind(n, parent), mod.name,
                                                  scope.self_cls, thing, site, "type"))
                return
            m = self.p.method_of(recv.q, n.attr)
            if m is not None:
                self.facts.method_uses.append(MethodUse(m[0].qualname, n.attr, mod.name, thing, site))
                self._ref(thing, mod, Symbol("class", m[0].qualname), n)
            return
        if not is_private(n.attr) or (isinstance(n.value, ast.Name) and n.value.id in {me, "self", "cls"}):
            return
        owners = self._private_owner.get(n.attr, [])
        if len(owners) == 1:
            self.facts.accesses.append(Access(owners[0], n.attr, _kind(n, parent), mod.name,
                                              scope.self_cls, thing, site, "name"))

    def _is_local_chain_expr(self, scope: Scope, e: ast.expr) -> bool:
        base = e
        while isinstance(base, ast.Attribute):
            base = base.value
        return isinstance(base, ast.Name) and self._is_local(scope, base.id)


def _kind(n: ast.Attribute, parent: dict[ast.AST, ast.AST]) -> AccessKind:
    if isinstance(n.ctx, (ast.Store, ast.Del)):
        return "write"
    p = parent.get(n)
    if isinstance(p, ast.Subscript) and p.value is n:
        return "write" if isinstance(p.ctx, (ast.Store, ast.Del)) else "index"
    if isinstance(p, ast.Attribute) and p.value is n:
        if isinstance(p.ctx, (ast.Store, ast.Del)):
            return "write"
        gp = parent.get(p)
        if isinstance(gp, ast.Call) and gp.func is p:
            return "call"
        return "reach"
    return "read"


def collect(project: Project) -> tuple[Facts, Typer]:
    c = Collector(project)
    return c.run(), c.typer
