"""The project as a set of parsed modules, and what each name in them refers to.

Names are resolved the way Python binds them at the top of a module: a
definition, an import (followed through re-exports such as a package's
``__init__``), or a plain alias assignment ``Client = _Client``. Only things
written in the project resolve; everything else is outside the analysis.
"""

from __future__ import annotations

import ast
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .discover import Config, SourceFile, find_sources

SymbolKind = Literal["module", "class", "function"]


@dataclass(frozen=True)
class Symbol:
    kind: SymbolKind
    qualname: str  # module name for modules, module.Name for definitions

    @property
    def module(self) -> str:
        return self.qualname if self.kind == "module" else self.qualname.rsplit(".", 1)[0]

    @property
    def name(self) -> str:
        return self.qualname.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class ImportBinding:
    """``from <module> import <name>`` (name set) or ``import <module>`` (name None)."""

    module: str
    name: str | None
    line: int


@dataclass(frozen=True)
class ImportRecord:
    """Every import statement, wherever it is written, for the module graph."""

    module: str
    names: tuple[str, ...]  # imported names for ``from`` imports, empty for ``import``
    line: int
    deferred: bool  # written inside a function
    type_only: bool  # written under ``if TYPE_CHECKING:``


@dataclass
class MethodInfo:
    name: str
    line: int
    node: ast.FunctionDef | ast.AsyncFunctionDef
    kind: Literal["method", "property", "static", "class"]
    wrapper: bool


@dataclass
class FieldInfo:
    name: str
    line: int
    annotation: ast.expr | None = None


@dataclass
class ClassInfo:
    qualname: str
    module: str
    name: str
    line: int
    node: ast.ClassDef
    bases: list[str] = field(default_factory=list)  # resolved project base classes
    external_bases: list[str] = field(default_factory=list)  # last identifier of the others
    fields: dict[str, FieldInfo] = field(default_factory=dict)
    methods: dict[str, MethodInfo] = field(default_factory=dict)
    decorators: list[str] = field(default_factory=list)


@dataclass
class FunctionInfo:
    qualname: str
    module: str
    name: str
    line: int
    node: ast.FunctionDef | ast.AsyncFunctionDef
    wrapper: bool


@dataclass
class Module:
    src: SourceFile
    tree: ast.Module
    defs: dict[str, Symbol] = field(default_factory=dict)
    imports: dict[str, ImportBinding] = field(default_factory=dict)
    aliases: dict[str, ast.expr] = field(default_factory=dict)
    records: list[ImportRecord] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.src.module

    @property
    def package(self) -> str:
        """The package relative imports are resolved against."""
        if self.src.is_package:
            return self.name
        return self.name.rsplit(".", 1)[0] if "." in self.name else ""


def last_ident(expr: ast.expr) -> str:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    if isinstance(expr, ast.Subscript):
        return last_ident(expr.value)
    if isinstance(expr, ast.Call):
        return last_ident(expr.func)
    return ""


def is_wrapper(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A pass-through: after the docstring, one statement that is just a call."""
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) != 1:
        return False
    stmt = body[0]
    value = stmt.value if isinstance(stmt, (ast.Return, ast.Expr)) else None
    if isinstance(value, ast.Await):
        value = value.value
    return isinstance(value, ast.Call)


def is_main_guard(stmt: ast.AST) -> bool:
    """``if __name__ == "__main__":``: script code, not part of the module's structure."""
    if not isinstance(stmt, ast.If) or not isinstance(stmt.test, ast.Compare):
        return False
    t = stmt.test
    sides = [t.left, *t.comparators]
    return (len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq)
            and any(isinstance(x, ast.Name) and x.id == "__name__" for x in sides)
            and any(isinstance(x, ast.Constant) and x.value == "__main__" for x in sides))


def _is_type_checking(test: ast.expr) -> bool:
    return last_ident(test) == "TYPE_CHECKING"


class Project:
    def __init__(self, root: Path, cfg: Config | None = None, sources: list[SourceFile] | None = None) -> None:
        self.root = root.resolve()
        self.cfg = cfg or Config.load(self.root)
        self.modules: dict[str, Module] = {}
        self.classes: dict[str, ClassInfo] = {}
        self.functions: dict[str, FunctionInfo] = {}
        self.parse_errors: list[str] = []
        for src in sources if sources is not None else find_sources(self.root, self.cfg):
            try:
                text = src.path.read_text(encoding="utf-8-sig")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    tree = ast.parse(text, filename=src.rel)
            except (SyntaxError, UnicodeDecodeError, ValueError, OSError) as exc:
                self.parse_errors.append(f"{src.rel}: {exc.__class__.__name__}")
                continue
            self.modules[src.module] = Module(src=src, tree=tree)
        for mod in self.modules.values():
            self._index(mod)
        for cls in self.classes.values():
            self._resolve_bases(cls)

    # -- indexing -----------------------------------------------------------------

    def absolute(self, mod: Module, module: str | None, level: int) -> str:
        if level == 0:
            return module or ""
        base = mod.package
        for _ in range(level - 1):
            base = base.rsplit(".", 1)[0] if "." in base else ""
        if module:
            return f"{base}.{module}" if base else module
        return base

    def _index(self, mod: Module) -> None:
        self._collect_records(mod, mod.tree.body, deferred=False, type_only=False)
        for stmt in mod.tree.body:
            self._bind_top(mod, stmt, type_only=False)

    def _collect_records(self, mod: Module, body: list[ast.stmt], deferred: bool, type_only: bool) -> None:
        for stmt in body:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    mod.records.append(ImportRecord(alias.name, (), stmt.lineno, deferred, type_only))
            elif isinstance(stmt, ast.ImportFrom):
                target = self.absolute(mod, stmt.module, stmt.level)
                names = tuple(a.name for a in stmt.names)
                mod.records.append(ImportRecord(target, names, stmt.lineno, deferred, type_only))
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._collect_records(mod, stmt.body, True, type_only)
            elif isinstance(stmt, ast.ClassDef):
                self._collect_records(mod, stmt.body, deferred, type_only)
            elif isinstance(stmt, ast.If):
                if is_main_guard(stmt):
                    continue
                tc = type_only or _is_type_checking(stmt.test)
                self._collect_records(mod, stmt.body, deferred, tc)
                self._collect_records(mod, stmt.orelse, deferred, type_only)
            elif isinstance(stmt, (ast.Try, ast.TryStar)):
                for part in (stmt.body, stmt.orelse, stmt.finalbody):
                    self._collect_records(mod, part, deferred, type_only)
                for h in stmt.handlers:
                    self._collect_records(mod, h.body, deferred, type_only)
            elif isinstance(stmt, (ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):
                self._collect_records(mod, stmt.body, deferred, type_only)
                self._collect_records(mod, getattr(stmt, "orelse", []), deferred, type_only)
            elif isinstance(stmt, ast.Match):
                for case in stmt.cases:
                    self._collect_records(mod, case.body, deferred, type_only)

    def _bind_top(self, mod: Module, stmt: ast.stmt, type_only: bool) -> None:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.asname:
                    mod.imports[alias.asname] = ImportBinding(alias.name, None, stmt.lineno)
                else:
                    head = alias.name.split(".", 1)[0]
                    mod.imports.setdefault(head, ImportBinding(head, None, stmt.lineno))
        elif isinstance(stmt, ast.ImportFrom):
            target = self.absolute(mod, stmt.module, stmt.level)
            for alias in stmt.names:
                if alias.name == "*":
                    continue
                mod.imports[alias.asname or alias.name] = ImportBinding(target, alias.name, stmt.lineno)
        elif isinstance(stmt, ast.ClassDef):
            q = f"{mod.name}.{stmt.name}"
            mod.defs[stmt.name] = Symbol("class", q)
            self.classes[q] = self._class_info(mod, stmt, q)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            q = f"{mod.name}.{stmt.name}"
            mod.defs[stmt.name] = Symbol("function", q)
            self.functions[q] = FunctionInfo(q, mod.name, stmt.name, stmt.lineno, stmt, is_wrapper(stmt))
        elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
            if isinstance(stmt.value, (ast.Name, ast.Attribute)):
                mod.aliases[stmt.targets[0].id] = stmt.value
        elif isinstance(stmt, ast.If) and not is_main_guard(stmt):
            tc = type_only or _is_type_checking(stmt.test)
            for s in stmt.body:
                self._bind_top(mod, s, tc)
            for s in stmt.orelse:
                self._bind_top(mod, s, type_only)
        elif isinstance(stmt, ast.Try):
            for s in stmt.body + stmt.orelse + stmt.finalbody:
                self._bind_top(mod, s, type_only)
            for h in stmt.handlers:
                for s in h.body:
                    self._bind_top(mod, s, type_only)

    def _class_info(self, mod: Module, node: ast.ClassDef, q: str) -> ClassInfo:
        info = ClassInfo(q, mod.name, node.name, node.lineno, node)
        info.decorators = [last_ident(d) for d in node.decorator_list]
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decos = {last_ident(d) for d in stmt.decorator_list}
                kind: Literal["method", "property", "static", "class"] = "method"
                if decos & {"property", "cached_property"} or any(
                    isinstance(d, ast.Attribute) and d.attr in {"setter", "getter", "deleter"} for d in stmt.decorator_list
                ):
                    kind = "property"
                elif "staticmethod" in decos:
                    kind = "static"
                elif "classmethod" in decos:
                    kind = "class"
                if stmt.name in info.methods and info.methods[stmt.name].kind == "property":
                    continue  # keep the getter of a property with a setter
                info.methods[stmt.name] = MethodInfo(stmt.name, stmt.lineno, stmt, kind, is_wrapper(stmt))
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if last_ident(stmt.annotation) != "ClassVar":
                    info.fields.setdefault(stmt.target.id, FieldInfo(stmt.target.id, stmt.lineno, stmt.annotation))
            elif isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__slots__" for t in stmt.targets):
                if isinstance(stmt.value, (ast.Tuple, ast.List)):
                    for elt in stmt.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            info.fields.setdefault(elt.value, FieldInfo(elt.value, stmt.lineno))
        for m in info.methods.values():
            if m.kind in ("static", "class") or not m.node.args.args:
                continue
            me = m.node.args.args[0].arg
            for sub in ast.walk(m.node):
                target: ast.expr | None = None
                ann: ast.expr | None = None
                if isinstance(sub, ast.AnnAssign):
                    target, ann = sub.target, sub.annotation
                elif isinstance(sub, (ast.Assign, ast.AugAssign)):
                    for t in sub.targets if isinstance(sub, ast.Assign) else [sub.target]:
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == me:
                            self._add_field(info, t, None)
                    continue
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == me:
                    self._add_field(info, target, ann)
        return info

    @staticmethod
    def _add_field(info: ClassInfo, target: ast.Attribute, ann: ast.expr | None) -> None:
        if target.attr in info.methods:
            return
        f = info.fields.get(target.attr)
        if f is None:
            info.fields[target.attr] = FieldInfo(target.attr, target.lineno, ann)
        elif f.annotation is None and ann is not None:
            f.annotation = ann

    def _resolve_bases(self, cls: ClassInfo) -> None:
        mod = self.modules[cls.module]
        for b in cls.node.bases:
            sym = self.resolve_expr(mod, b)
            if sym is not None and sym.kind == "class":
                cls.bases.append(sym.qualname)
            else:
                cls.external_bases.append(last_ident(b))

    # -- resolution ---------------------------------------------------------------

    def resolve(self, module: str, name: str, _seen: frozenset[tuple[str, str]] = frozenset()) -> Symbol | None:
        """What ``name`` at the top of ``module`` refers to, if it is in the project."""
        if (module, name) in _seen:
            return None
        seen = _seen | {(module, name)}
        mod = self.modules.get(module)
        if mod is None:
            return None
        if name in mod.defs:
            return mod.defs[name]
        if name in mod.imports:
            return self.resolve_binding(mod.imports[name], seen)
        if name in mod.aliases:
            return self.resolve_expr(mod, mod.aliases[name], seen)
        sub = f"{module}.{name}"
        if sub in self.modules:
            return Symbol("module", sub)
        return None

    def resolve_binding(self, b: ImportBinding, seen: frozenset[tuple[str, str]] = frozenset()) -> Symbol | None:
        if b.name is None:
            return Symbol("module", b.module) if b.module in self.modules else None
        sym = self.resolve(b.module, b.name, seen)
        if sym is not None:
            return sym
        sub = f"{b.module}.{b.name}" if b.module else b.name
        return Symbol("module", sub) if sub in self.modules else None

    def resolve_expr(self, mod: Module, expr: ast.expr, seen: frozenset[tuple[str, str]] = frozenset(),
                     local: dict[str, ImportBinding] | None = None) -> Symbol | None:
        if isinstance(expr, ast.Name):
            if local and expr.id in local:
                return self.resolve_binding(local[expr.id], seen)
            return self.resolve(mod.name, expr.id, seen)
        if isinstance(expr, ast.Attribute):
            base = self.resolve_expr(mod, expr.value, seen, local)
            if base is not None and base.kind == "module":
                return self.resolve(base.qualname, expr.attr, seen)
        return None

    # -- class hierarchy ----------------------------------------------------------

    def mro(self, qualname: str) -> list[ClassInfo]:
        out: list[ClassInfo] = []
        seen: set[str] = set()
        todo = [qualname]
        while todo:
            q = todo.pop(0)
            c = self.classes.get(q)
            if c is None or q in seen:
                continue
            seen.add(q)
            out.append(c)
            todo.extend(c.bases)
        return out

    def is_subclass(self, sub: str, sup: str) -> bool:
        return any(c.qualname == sup for c in self.mro(sub))

    def field_owner(self, cls: str, attr: str) -> ClassInfo | None:
        for c in self.mro(cls):
            if attr in c.methods:
                return None
            if attr in c.fields:
                return c
        return None

    def method_of(self, cls: str, attr: str) -> tuple[ClassInfo, MethodInfo] | None:
        for c in self.mro(cls):
            if attr in c.methods:
                return c, c.methods[attr]
        return None
