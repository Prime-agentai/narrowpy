from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from conftest import keys
from narrowpy.findings import Finding
from narrowpy.lints import analyze
import sloppy

Run = Callable[..., list[Finding]]


def test_sloppy_app_reports_one_of_each(tmp_path: Path) -> None:
    findings = analyze(sloppy.write(tmp_path)).findings
    assert keys(findings) == [
        "field-leak:app.queue.RunQueue._seen",
        "field-leak:app.queue.RunQueue.head",
        "field-leak:app.queue.RunQueue.items",
        "module-cycle:app.cli|app.store",
    ]
    cycle = next(f for f in findings if f.lint == "module-cycle")
    assert any("only the hidden imports close it" in n for n in cycle.notes)
    assert any("app/store.py:5" in n and "hidden" in n for n in cycle.notes)
    items = next(f for f in findings if f.key.endswith(".items"))
    assert items.metric == 2 and "2 places in 1 module" in items.message
    # report.py only reads: len(q.items), q.head, q.limit. Reading is no leak.
    assert all("app/report.py" not in n for f in findings for n in f.notes)


def test_type_checking_import_still_closes_a_cycle(project: Run) -> None:
    found = project({
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg.b import g\ndef f() -> int:\n    return g()\n",
        "pkg/b.py": ("from __future__ import annotations\nfrom typing import TYPE_CHECKING\n"
                     "if TYPE_CHECKING:\n    from pkg.a import f\ndef g() -> int:\n    return 1\n"),
    })
    (cycle,) = found
    assert cycle.key == "module-cycle:pkg.a|pkg.b"
    assert any("TYPE_CHECKING" in row for row in cycle.picture)


def test_runtime_cycle_is_named_as_such(project: Run) -> None:
    (cycle,) = project({
        "pkg/__init__.py": "",
        "pkg/a.py": "import pkg.b\nX = 1\n",
        "pkg/b.py": "import pkg.a\nY = 2\n",
    })
    assert any("at import time" in n and "2 of these modules" in n for n in cycle.notes)


def test_package_and_its_children_make_no_cycle(project: Run) -> None:
    assert project({
        "pkg/__init__.py": "from pkg.core import Engine\n__all__ = ['Engine']\n",
        "pkg/core.py": "from pkg import util\nclass Engine:\n    pass\n",
        "pkg/util/__init__.py": "def helper() -> int:\n    return 1\n",
    }, lints=["module-cycle"]) == []


def test_cycle_is_lifted_to_siblings(project: Run) -> None:
    (cycle,) = project({
        "app/__init__.py": "", "app/x/__init__.py": "", "app/y/__init__.py": "",
        "app/x/m.py": "from app.y.n import B\nclass A:\n    pass\n",
        "app/y/n.py": "def make():\n    from app.x.m import A\n    return A()\nclass B:\n    pass\n",
    }, lints=["module-cycle"])
    assert cycle.key == "module-cycle:app.x|app.y"


def test_reexports_resolve_to_the_defining_module(project: Run) -> None:
    (cycle,) = project({
        "app/__init__.py": "", "app/models/__init__.py": "from .user import User\n",
        "app/models/user.py": "from app.services.mail import send\nclass User:\n    pass\n",
        "app/services/__init__.py": "",
        "app/services/mail.py": "from app.models import User\ndef send(u: User) -> None:\n    pass\n",
    }, lints=["module-cycle"])
    assert cycle.key == "module-cycle:app.models|app.services"


def test_main_guard_is_script_code(project: Run) -> None:
    assert project({
        "pkg/__init__.py": "",
        "pkg/a.py": "from pkg.b import g\n",
        "pkg/b.py": "def g() -> int:\n    return 1\nif __name__ == '__main__':\n    from pkg import a\n",
    }) == []


LEAKY = {
    "shop/__init__.py": "",
    "shop/cart.py": '''
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Money:
    cents: int

    def plus(self, other: Money) -> Money:
        return Money(self.cents + other.cents)


@dataclass
class Line:
    sku: str
    qty: int


class Cart:
    owner: str

    def __init__(self, owner: str, label: str) -> None:
        self.owner = owner
        self.label = label.strip()
        self.lines: list[Line] = []
        self.total = Money(0)
        self._discount = 0

    def add(self, line: Line) -> None:
        self.lines.append(line)

    def _helper(self) -> int:
        return self._discount


def apply_discount(cart: Cart, pct: int) -> None:
    cart._discount = pct      # same module: an insider
''',
    "shop/checkout.py": '''
from __future__ import annotations
from typing import Optional
from shop.cart import Cart, Line


def checkout(cart: Optional[Cart], extra: list[Cart]) -> int:
    assert cart is not None
    cart.lines.append(Line("x", 1))        # call on a field: leak
    n = len(cart.lines)                    # read: fine
    print(cart.owner.upper(), cart.label.lower())  # value fields: reads
    print(cart.total.plus(cart.total))     # immutable value: read
    print(cart.total.cents)                # reaching through to read: fine
    for other in extra:
        other.lines[0] = Line("y", 2)      # write through index: leak
    line = Line("z", 3)
    line.qty += 1                          # a record: never a leak
    return n + cart._discount              # private: leak even when read


class Premium(Cart):
    def bump(self) -> None:
        self.lines.clear()                 # subclass: an insider
''',
}


def test_field_leak_rules(project: Run) -> None:
    found = project(LEAKY, lints=["field-leak"])
    assert keys(found) == ["field-leak:shop.cart.Cart._discount", "field-leak:shop.cart.Cart.lines"]
    lines = next(f for f in found if f.key.endswith(".lines"))
    assert lines.metric == 2
    assert {n.split(": ")[1].split(" ")[0] for n in lines.notes} == {"call", "write"}
    private = next(f for f in found if f.key.endswith("._discount"))
    assert "(private field)" in private.message and private.metric == 1


def test_data_structure_with_helpers_is_left_alone(project: Run) -> None:
    assert project({
        "p/__init__.py": "",
        "p/a.py": ("class Box:\n    def __init__(self) -> None:\n        self.items: list[int] = []\n"
                   "    def size(self) -> int:\n        return len(self.items)\n"),
        "p/b.py": "from p.a import Box\ndef f(b: Box) -> None:\n    b.items.append(1)\n",
    }, lints=["field-leak"]) == []


def test_private_field_found_by_unique_name(project: Run) -> None:
    (leak,) = project({
        "p/__init__.py": "",
        "p/a.py": ("class Pool:\n    def __init__(self) -> None:\n        self._conns: list[int] = []\n"
                   "    def take(self) -> int:\n        return self._conns.pop()\n"),
        "p/b.py": "def drain(pool):\n    pool._conns.clear()\n",
    }, lints=["field-leak"])
    assert leak.key == "field-leak:p.a.Pool._conns"
    assert "inferred from the unique field name" in leak.notes[0]


def test_receivers_typed_through_returns_iteration_and_context(project: Run) -> None:
    found = project({
        "q/__init__.py": "",
        "q/core.py": '''
from __future__ import annotations
from typing import Self


class Conn:
    def __init__(self) -> None:
        self.buf: list[bytes] = []
        self._open = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *a: object) -> None:
        self._open = False

    def send(self, b: bytes) -> None:
        self.buf.append(b)


def connect() -> "Conn":
    return Conn()


class Hub:
    def __init__(self) -> None:
        self.conns: dict[str, Conn] = {}

    def get(self, k: str) -> Conn:
        return self.conns[k]
''',
        "q/use.py": '''
from q.core import Hub, connect


def a() -> None:
    connect().buf.clear()


def b(hub: Hub) -> None:
    for c in hub.conns.values():
        c.buf.append(b"x")
    hub.get("k").buf.pop()


def c() -> None:
    with connect() as conn:
        conn.buf.extend([])
''',
    }, lints=["field-leak"])
    keys_ = keys(found)
    assert "field-leak:q.core.Conn.buf" in keys_
    buf = next(f for f in found if f.key.endswith("Conn.buf"))
    assert buf.metric == 4  # connect().buf, c.buf via dict values, hub.get().buf, with-as
    # Hub lets its only field out, so it is a data structure with helpers, not a guard.
    assert "field-leak:q.core.Hub.conns" not in keys_


def test_tangle_between_package_and_child(project: Run) -> None:
    found = project({
        "g/__init__.py": "from g.node import Node\nclass Graph:\n    def add(self) -> 'Node':\n        return Node(self)\n",
        "g/node.py": "from __future__ import annotations\nimport g\nclass Node:\n    def __init__(self, owner: g.Graph) -> None:\n        self.owner = owner\n",
    }, lints=["tangle"])
    (t,) = found
    assert t.key == "tangle:g.Graph|g.node.Node"
    assert "package to a module inside it" in t.notes[0]


def test_shallow_module(project: Run) -> None:
    funcs = "\n".join(f"def f{i}(x: int) -> int:\n    y = x + {i}\n    return y\n" for i in range(9))
    funcs += "\ndef wrapper(x: int) -> int:\n    return f0(x)\n"
    calls = "\n".join(f"    t += u.f{i}(1)" for i in range(9))
    found = project({
        "s/__init__.py": "",
        "s/util.py": funcs,
        "s/app.py": f"from s import util\ndef main() -> int:\n    t = 0\n{calls}\n    t += util.wrapper(1)\n    return t\n".replace("u.f", "util.f"),
    }, lints=["shallow-module"])
    assert keys(found) == ["shallow-module:s.util"]
    assert "shows 9 functions and hides 0" in found[0].message  # the wrapper does no work: not counted


def test_small_interface_is_fine(project: Run) -> None:
    funcs = "\n".join(f"def f{i}(x: int) -> int:\n    y = x\n    return y\n" for i in range(5))
    assert project({
        "s/__init__.py": "", "s/util.py": funcs,
        "s/app.py": "from s.util import f0, f1, f2, f3, f4\nprint(f0(1), f1(1), f2(1), f3(1), f4(1))\n",
    }, lints=["shallow-module"]) == []


def test_tests_and_examples_are_outsiders_by_default(project: Run) -> None:
    files = {
        "p/__init__.py": "",
        "p/a.py": ("class Pool:\n    def __init__(self) -> None:\n        self._n = 0\n"
                   "    def take(self) -> int:\n        return self._n\n"),
        "tests/test_a.py": "from p.a import Pool\ndef test() -> None:\n    Pool()._n = 3\n",
        "examples/demo.py": "from p.a import Pool\nPool()._n = 4\n",
    }
    assert project(files) == []
    assert keys(project(files, include_tests=True)) == ["field-leak:p.a.Pool._n"]


def test_syntax_errors_are_skipped_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "bad.py").write_text("def (:\n")
    a = analyze(tmp_path)
    assert a.findings == [] and a.parse_errors == ["bad.py: SyntaxError"]
