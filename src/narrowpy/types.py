"""Just enough typing to know which project class a receiver is.

This is not a type checker. It reads annotations, constructor calls, the
return annotations of project functions and methods, and the fields a class
assigns, inside one scope at a time. A name gets a type only when every place
that binds it agrees; anything unclear is left unknown, so a lint built on top
reports less rather than guessing.
"""

from __future__ import annotations

import ast
import builtins
import functools
from dataclasses import dataclass

from .hierarchy import lineage, member
from .project import ClassInfo, ImportBinding, MethodInfo, Module, Project, is_main_guard, last_ident


@dataclass(frozen=True)
class ClassTy:
    q: str


@dataclass(frozen=True)
class IterTy:
    elem: Ty


@dataclass(frozen=True)
class MapTy:
    value: Ty


Ty = ClassTy | IterTy | MapTy

ITER_NAMES = {
    "list", "List", "set", "Set", "frozenset", "FrozenSet", "Sequence", "MutableSequence", "Iterable",
    "Iterator", "Collection", "AbstractSet", "MutableSet", "deque", "Deque", "Generator", "AsyncIterator",
    "AsyncIterable", "AsyncGenerator", "KeysView", "ValuesView", "Reversible",
}
MAP_NAMES = {"dict", "Dict", "Mapping", "MutableMapping", "defaultdict", "DefaultDict", "OrderedDict", "ChainMap", "Counter"}
UNWRAP_FIRST = {"Optional", "Annotated", "Final", "ClassVar", "Required", "NotRequired", "ReadOnly"}
UNWRAP_LAST = {"Awaitable", "Coroutine"}
#: Fields of these types hold plain values: calling a method on one or
#: indexing it reads the value, it does not reach into the owner.
VALUE_TYPES = {
    "str", "int", "float", "bool", "bytes", "complex", "Decimal", "Fraction", "datetime", "date", "time",
    "timedelta", "Path", "PurePath", "PosixPath", "WindowsPath", "UUID", "Enum", "IntEnum", "StrEnum", "Literal",
}


BUILTIN_NAMES = set(dir(builtins))
#: Methods that build an object; setting fields in them does not make it mutable.
BUILD_METHODS = {"__init__", "__new__", "__post_init__", "__setstate__", "__init_subclass__", "__set_name__"}


@dataclass
class Scope:
    mod: Module
    env: dict[str, Ty | None]
    self_cls: str | None
    local_imports: dict[str, ImportBinding]
    outer: Scope | None = None

    def lookup(self, name: str) -> tuple[bool, Ty | None]:
        s: Scope | None = self
        while s is not None:
            if name in s.env:
                return True, s.env[name]
            s = s.outer
        return False, None

    def imports(self) -> dict[str, ImportBinding]:
        out: dict[str, ImportBinding] = {}
        chain: list[Scope] = []
        s: Scope | None = self
        while s is not None:
            chain.append(s)
            s = s.outer
        for sc in reversed(chain):
            out.update(sc.local_imports)
        return out


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        return body[1:]
    return body


@functools.lru_cache(maxsize=None)
def own_nodes(root: ast.AST) -> tuple[ast.AST, ...]:
    """Every node under ``root`` that is not inside a nested def, class or lambda (parents first)."""
    return tuple(_own_nodes(root))


def _own_nodes(root: ast.AST) -> list[ast.AST]:
    out: list[ast.AST] = []
    todo = list(ast.iter_child_nodes(root))
    while todo:
        n = todo.pop()
        out.append(n)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        todo.extend(ast.iter_child_nodes(n))
    return out


class Typer:
    def __init__(self, project: Project) -> None:
        self.p = project
        self._field_cache: dict[tuple[str, str], Ty | None] = {}
        self._field_busy: set[tuple[str, str]] = set()
        self._module_scopes: dict[str, Scope] = {}
        self._method_scopes: dict[tuple[str, str], Scope] = {}
        self._assign_cache: dict[str, dict[str, list[tuple[MethodInfo, ast.expr]]]] = {}
        self._immutable: dict[str, bool] = {}

    # -- annotations ----------------------------------------------------------------

    def ann(self, mod: Module, expr: ast.expr | None, self_cls: str | None,
            local: dict[str, ImportBinding] | None = None) -> Ty | None:
        if expr is None:
            return None
        if isinstance(expr, ast.Constant):
            if isinstance(expr.value, str):
                parsed = parse_annotation(expr.value)
                return self.ann(mod, parsed, self_cls, local) if parsed is not None else None
            return None
        if isinstance(expr, (ast.Name, ast.Attribute)):
            if last_ident(expr) == "Self":
                return ClassTy(self_cls) if self_cls else None
            sym = self.p.resolve_expr(mod, expr, local=local)
            return ClassTy(sym.qualname) if sym is not None and sym.kind == "class" else None
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            return self._union(mod, [expr.left, expr.right], self_cls, local)
        if isinstance(expr, ast.Subscript):
            head = last_ident(expr.value)
            args = list(expr.slice.elts) if isinstance(expr.slice, ast.Tuple) else [expr.slice]
            if head in UNWRAP_FIRST:
                return self.ann(mod, args[0], self_cls, local)
            if head in UNWRAP_LAST:
                return self.ann(mod, args[-1], self_cls, local)
            if head == "Union":
                return self._union(mod, args, self_cls, local)
            if head in ITER_NAMES or (head in {"tuple", "Tuple"} and len(args) == 2
                                      and isinstance(args[1], ast.Constant) and args[1].value is Ellipsis):
                elem = self.ann(mod, args[0], self_cls, local)
                return IterTy(elem) if elem is not None else None
            if head in MAP_NAMES:
                value = self.ann(mod, args[-1], self_cls, local)
                return MapTy(value) if value is not None else None
        return None

    def _union(self, mod: Module, parts: list[ast.expr], self_cls: str | None,
               local: dict[str, ImportBinding] | None) -> Ty | None:
        found: list[Ty] = []
        for part in parts:
            if isinstance(part, ast.Constant) and part.value is None or last_ident(part) == "None":
                continue
            if isinstance(part, ast.BinOp) and isinstance(part.op, ast.BitOr):
                t = self._union(mod, [part.left, part.right], self_cls, local)
            else:
                t = self.ann(mod, part, self_cls, local)
            if t is None:
                return None
            found.append(t)
        return found[0] if found and all(t == found[0] for t in found) else None

    def is_value_annotation(self, expr: ast.expr | None) -> bool:
        if expr is None:
            return False
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            parsed = parse_annotation(expr.value)
            if parsed is None:
                return False
            expr = parsed
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
            return all(self.is_value_annotation(x) or last_ident(x) == "None" or
                       (isinstance(x, ast.Constant) and x.value is None) for x in (expr.left, expr.right))
        if isinstance(expr, ast.Subscript) and last_ident(expr.value) in {"Optional", "Final", "Annotated", "ClassVar", "Literal"}:
            if last_ident(expr.value) == "Literal":
                return True
            inner = expr.slice.elts[0] if isinstance(expr.slice, ast.Tuple) else expr.slice
            return self.is_value_annotation(inner)
        return last_ident(expr) in VALUE_TYPES

    # -- fields ---------------------------------------------------------------------

    def field_type(self, cls_q: str, attr: str) -> Ty | None:
        cls = self.p.find_class(cls_q)
        found = member(cls, attr) if cls is not None else None
        if found is None:
            return None
        if found.method is not None:
            if found.method.kind == "property":
                return self.ann(found.owner.mod, found.method.node.returns, cls_q)
            return None
        owner = found.owner
        key = (owner.qualname, attr)
        if key in self._field_cache:
            return self._field_cache[key]
        if key in self._field_busy:
            return None
        self._field_busy.add(key)
        try:
            t = self._compute_field_type(owner, attr)
        finally:
            self._field_busy.discard(key)
        self._field_cache[key] = t
        return t

    def _compute_field_type(self, owner: ClassInfo, attr: str) -> Ty | None:
        mod = owner.mod
        f = owner.fields[attr]
        if f.annotation is not None:
            return self.ann(mod, f.annotation, owner.qualname)
        found: list[Ty | None] = []
        for m, value in self.self_assignments(owner).get(attr, []):
            if isinstance(value, ast.Constant) and value.value is None:
                continue
            found.append(self.expr(self.method_scope(owner, m.name), value))
        if found and all(t is not None and t == found[0] for t in found):
            return found[0]
        return None

    def field_is_value(self, owner: ClassInfo, attr: str) -> bool:
        f = owner.fields.get(attr)
        if f is None:
            return False
        if f.annotation is not None:
            return self.is_value_annotation(f.annotation)
        values: list[bool] = []
        for m, v in self.self_assignments(owner).get(attr, []):
            params = {a.arg: a.annotation for a in m.node.args.args + m.node.args.kwonlyargs}
            if isinstance(v, ast.Constant):
                if v.value is not None:
                    values.append(True)
            elif isinstance(v, ast.JoinedStr):
                values.append(True)
            else:
                values.append(self._derived_from_values(v, params))
        return bool(values) and all(values)

    def self_assignments(self, owner: ClassInfo) -> dict[str, list[tuple[MethodInfo, ast.expr]]]:
        """``self.<field> = <value>`` in the class's own methods, by field."""
        cached = self._assign_cache.get(owner.qualname)
        if cached is not None:
            return cached
        out: dict[str, list[tuple[MethodInfo, ast.expr]]] = {}
        for m in owner.methods.values():
            if m.kind in ("static", "class") or not m.node.args.args:
                continue
            me = m.node.args.args[0].arg
            for n in own_nodes(m.node):
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == me:
                            out.setdefault(t.attr, []).append((m, n.value))
        self._assign_cache[owner.qualname] = out
        return out

    @staticmethod
    def _value_params(params: dict[str, ast.expr | None], typer: Typer) -> set[str]:
        return {k for k, a in params.items() if typer.is_value_annotation(a)}

    def _derived_from_values(self, v: ast.expr, params: dict[str, ast.expr | None]) -> bool:
        """``method.upper()`` where ``method: str``: built only from value parameters and builtins."""
        names = {n.id for n in ast.walk(v) if isinstance(n, ast.Name)}
        values = self._value_params(params, self)
        return bool(names & values) and names <= values | BUILTIN_NAMES

    def is_immutable(self, cls_q: str) -> bool:
        """A frozen dataclass, NamedTuple or enum, or a class that only sets fields while being built."""
        if cls_q not in self._immutable:
            self._immutable[cls_q] = self._compute_immutable(cls_q)
        return self._immutable[cls_q]

    def _compute_immutable(self, cls_q: str) -> bool:
        cls = self.p.find_class(cls_q)
        if cls is None:
            return False
        chain = lineage(cls)
        for c in chain:
            if set(c.external_bases) & {"NamedTuple", "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}:
                return True
            for d in c.node.decorator_list:
                if isinstance(d, ast.Call) and last_ident(d.func) == "dataclass" and any(
                        k.arg == "frozen" and isinstance(k.value, ast.Constant) and k.value.value is True for k in d.keywords):
                    return True
        for c in chain:
            for m in c.methods.values():
                if m.name in BUILD_METHODS or m.kind in ("static", "class") or not m.node.args.args:
                    continue
                me = m.node.args.args[0].arg
                for n in ast.walk(m.node):
                    target = n.value if isinstance(n, ast.Subscript) else n
                    if isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, (ast.Store, ast.Del)):
                        while isinstance(target, (ast.Attribute, ast.Subscript)):
                            if isinstance(target.value, ast.Name) and target.value.id == me:
                                return False
                            target = target.value
        return bool(chain[0].fields or chain[0].methods)

    # -- scopes ---------------------------------------------------------------------

    def module_scope(self, mod: Module) -> Scope:
        s = self._module_scopes.get(mod.name)
        if s is None:
            s = Scope(mod, {}, None, {})
            self._module_scopes[mod.name] = s
            s.env.update(self._infer(s, mod.tree, params={}))
        return s

    def method_scope(self, cls: ClassInfo, name: str) -> Scope:
        key = (cls.qualname, name)
        s = self._method_scopes.get(key)
        if s is None:
            m = cls.methods[name]
            s = self.function_scope(cls.mod, m.node, cls.qualname, None)
            self._method_scopes[key] = s
        return s

    def function_scope(self, mod: Module, fn: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda,
                       self_cls: str | None, outer: Scope | None) -> Scope:
        s = Scope(mod, {}, self_cls, {}, outer or self.module_scope(mod))
        params: dict[str, Ty | None] = {}
        a = fn.args
        positional = a.posonlyargs + a.args
        is_static = isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            last_ident(d) in {"staticmethod", "classmethod"} for d in fn.decorator_list)
        for i, arg in enumerate(positional + a.kwonlyargs):
            if i == 0 and self_cls and not is_static and arg.annotation is None and arg in positional:
                params[arg.arg] = ClassTy(self_cls)
            else:
                params[arg.arg] = self.ann(mod, arg.annotation, self_cls, s.imports()) if arg.annotation is not None else None
        for extra in (a.vararg, a.kwarg):
            if extra is not None:
                params[extra.arg] = None
        s.local_imports.update(self.p.local_imports(mod, own_nodes(fn)))
        s.env.update(params)
        s.env.update(self._infer(s, fn, params))
        return s

    def _infer(self, s: Scope, root: ast.AST, params: dict[str, Ty | None]) -> dict[str, Ty | None]:
        """Flow-insensitive: a local gets a type only if all its bindings agree."""
        events: list[tuple[str, str, ast.expr | None]] = []

        def bind(target: ast.expr, kind: str, expr: ast.expr | None) -> None:
            if isinstance(target, ast.Name):
                events.append((target.id, kind, expr))
            elif isinstance(target, (ast.Tuple, ast.List)):
                for elt in target.elts:
                    bind(elt.value if isinstance(elt, ast.Starred) else elt, "unknown", None)

        nodes = own_nodes(root) if not isinstance(root, ast.Module) else module_nodes(root)
        for n in nodes:
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    bind(t, "value", n.value)
            elif isinstance(n, ast.AnnAssign):
                bind(n.target, "ann", n.annotation)
            elif isinstance(n, ast.NamedExpr):
                bind(n.target, "value", n.value)
            elif isinstance(n, (ast.For, ast.AsyncFor)):
                bind(n.target, "iter", n.iter)
            elif isinstance(n, ast.comprehension):
                bind(n.target, "iter", n.iter)
            elif isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    if item.optional_vars is not None:
                        bind(item.optional_vars, "enter", item.context_expr)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                events.append((n.name, "ann", n.type))
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not isinstance(root, ast.Module):
                events.append((n.name, "unknown", None))
        names = {e[0] for e in events} - set(params)
        env: dict[str, Ty | None] = {name: None for name in names}
        s.env.update(env)
        for _ in range(3):
            changed = False
            for name in names:
                results: list[Ty | None] = []
                for ename, kind, expr in events:
                    if ename != name:
                        continue
                    if kind == "value" and isinstance(expr, ast.Constant) and expr.value is None:
                        continue
                    results.append(self._event_type(s, kind, expr))
                agreed = results[0] if results and all(r is not None and r == results[0] for r in results) else None
                if s.env.get(name) != agreed:
                    s.env[name] = agreed
                    changed = True
            if not changed:
                break
        return {name: s.env[name] for name in names}

    def _event_type(self, s: Scope, kind: str, expr: ast.expr | None) -> Ty | None:
        if expr is None or kind == "unknown":
            return None
        if kind == "ann":
            return self.ann(s.mod, expr, s.self_cls, s.imports())
        t = self.expr(s, expr)
        if kind == "value":
            return t
        if kind == "iter":
            return t.elem if isinstance(t, IterTy) else None
        if kind == "enter" and isinstance(t, ClassTy):
            found = self._method(t.q, "__enter__") or self._method(t.q, "__aenter__")
            if found is None:
                return None
            c, m = found
            if m.node.returns is not None:
                return self.ann(c.mod, m.node.returns, t.q)
            body = _strip_docstring(m.node.body)
            if body and isinstance(body[-1], ast.Return) and isinstance(body[-1].value, ast.Name) and m.node.args.args \
                    and body[-1].value.id == m.node.args.args[0].arg:
                return t
        return None

    # -- expressions ----------------------------------------------------------------

    def expr(self, s: Scope, e: ast.expr) -> Ty | None:
        if isinstance(e, ast.Name):
            found, t = s.lookup(e.id)
            return t if found else None
        if isinstance(e, ast.Await):
            return self.expr(s, e.value)
        if isinstance(e, ast.NamedExpr):
            return self.expr(s, e.value)
        if isinstance(e, ast.Attribute):
            recv = self.expr(s, e.value)
            if isinstance(recv, ClassTy):
                return self.field_type(recv.q, e.attr)
            return None
        if isinstance(e, ast.Subscript):
            recv = self.expr(s, e.value)
            if isinstance(recv, IterTy):
                return recv if isinstance(e.slice, ast.Slice) else recv.elem
            if isinstance(recv, MapTy):
                return recv.value
            return None
        if isinstance(e, ast.IfExp):
            a, b = self.expr(s, e.body), self.expr(s, e.orelse)
            if isinstance(e.orelse, ast.Constant) and e.orelse.value is None:
                return a
            return a if a == b else None
        if isinstance(e, ast.BoolOp):
            ts = [self.expr(s, v) for v in e.values]
            return ts[0] if ts and all(t is not None and t == ts[0] for t in ts) else None
        if isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            t = self.expr(s, e.elt)
            return IterTy(t) if t is not None else None
        if isinstance(e, (ast.List, ast.Tuple, ast.Set)) and e.elts:
            ts = [self.expr(s, v) for v in e.elts]
            return IterTy(ts[0]) if ts[0] is not None and all(t == ts[0] for t in ts) else None
        if isinstance(e, ast.Call):
            return self._call(s, e)
        return None

    def _call(self, s: Scope, e: ast.Call) -> Ty | None:
        f = e.func
        found, local_t = s.lookup(f.id) if isinstance(f, ast.Name) else (False, None)
        if not found or local_t is None:
            sym = self.p.resolve_expr(s.mod, f, local=s.imports())
            if sym is not None:
                if sym.kind == "class":
                    return ClassTy(sym.qualname)
                if sym.kind == "function":
                    fn = self.p.find_function(sym.qualname)
                    return self.ann(fn.mod, fn.node.returns, None) if fn is not None else None
                return None
        if isinstance(f, ast.Name):
            if f.id in {"list", "sorted", "reversed", "tuple", "set", "frozenset", "iter"} and e.args:
                t = self.expr(s, e.args[0])
                return t if isinstance(t, IterTy) else None
            if f.id == "next" and e.args:
                t = self.expr(s, e.args[0])
                return t.elem if isinstance(t, IterTy) else None
            if f.id == "cast" and e.args:
                return self.ann(s.mod, e.args[0], s.self_cls, s.imports())
            return None
        if isinstance(f, ast.Attribute):
            cls_sym = self.p.resolve_expr(s.mod, f.value, local=s.imports())
            if cls_sym is not None and cls_sym.kind == "class":
                return self._method_return(cls_sym.qualname, f.attr)
            recv = self.expr(s, f.value)
            if isinstance(recv, ClassTy):
                return self._method_return(recv.q, f.attr)
            if isinstance(recv, MapTy):
                if f.attr in {"get", "pop", "setdefault"}:
                    return recv.value
                if f.attr == "values":
                    return IterTy(recv.value)
            if isinstance(recv, IterTy):
                if f.attr == "pop":
                    return recv.elem
                if f.attr == "copy":
                    return recv
        return None

    def _method_return(self, cls_q: str, name: str) -> Ty | None:
        found = self._method(cls_q, name)
        if found is None:
            return None
        c, m = found
        return self.ann(c.mod, m.node.returns, cls_q)

    def _method(self, cls_q: str, name: str) -> tuple[ClassInfo, MethodInfo] | None:
        cls = self.p.find_class(cls_q)
        found = member(cls, name) if cls is not None else None
        if found is None or found.method is None:
            return None
        return found.owner, found.method


@functools.lru_cache(maxsize=None)
def module_nodes(tree: ast.Module) -> tuple[ast.AST, ...]:
    """Module-level nodes outside any def, class, lambda or ``if __name__ == "__main__":`` block."""
    out: list[ast.AST] = []
    todo: list[ast.AST] = [s for s in tree.body if not is_main_guard(s)]
    while todo:
        n = todo.pop()
        out.append(n)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        todo.extend(ast.iter_child_nodes(n))
    return tuple(out)


@functools.lru_cache(maxsize=4096)
def parse_annotation(text: str) -> ast.expr | None:
    try:
        return ast.parse(text, mode="eval").body
    except SyntaxError:
        return None
