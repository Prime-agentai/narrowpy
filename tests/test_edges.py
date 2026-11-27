"""Edge cases found by probing: how receivers get (or do not get) a type, and how files get module names."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import keys, write_files
from narrowpy.discover import Config, find_sources, module_name
from narrowpy.findings import Finding

Run = Callable[..., list[Finding]]

POOL = "p.a.Pool.conns"
LIB = {
    "p/__init__.py": "",
    "p/a.py": ("class Pool:\n    def __init__(self) -> None:\n        self.conns: list[int] = []\n        self.n = 0\n"
               "    def take(self) -> int:\n        return self.conns.pop()\n"),
    "p/other.py": "class Other:\n    def __init__(self) -> None:\n        self.conns: list[int] = []\n    def get(self) -> int:\n        return 1\n",
}

# Receiver typing: each body is the whole of p/b.py. Expected: the field that leaks, or None for "say nothing".
RECEIVERS = {
    "two types on one name": ("from p.a import Pool\nfrom p.other import Other\ndef f(flag: bool):\n    x = Pool() if flag else Other()\n    x.conns.append(1)\n", None),
    "rebound to another type": ("from p.a import Pool\nfrom p.other import Other\ndef f():\n    x = Pool()\n    x = Other()\n    x.conns.append(1)\n", None),
    "import alias": ("from p.a import Pool as P\ndef f(x: P):\n    x.conns.append(1)\n", POOL),
    "module alias attribute": ("import p.a as pa\ndef f(x: pa.Pool):\n    x.conns.append(1)\n", POOL),
    "relative import": ("from .a import Pool\ndef f(x: Pool):\n    x.conns.append(1)\n", POOL),
    "string annotation": ("from p.a import Pool\ndef f(x: 'Pool'):\n    x.conns.append(1)\n", POOL),
    "Optional": ("from typing import Optional\nfrom p.a import Pool\ndef f(x: Optional[Pool]):\n    x.conns.append(1)\n", POOL),
    "X | None": ("from p.a import Pool\ndef f(x: Pool | None):\n    x.conns.append(1)\n", POOL),
    "walrus": ("from p.a import Pool\ndef f():\n    if (x := Pool()):\n        x.conns.append(1)\n", POOL),
    "comprehension variable": ("from p.a import Pool\ndef f(xs: list[Pool]):\n    [x.conns.append(1) for x in xs]\n", POOL),
    "field of a field": ("from p.a import Pool\nclass U:\n    def __init__(self) -> None:\n        self.pool = Pool()\n    def g(self):\n        self.pool.conns.append(1)\n", POOL),
    "del item": ("from p.a import Pool\ndef f(x: Pool):\n    del x.conns[0]\n", POOL),
    "local class shadows the import": ("from p.a import Pool\ndef f():\n    class Pool:\n        conns = []\n    Pool.conns.append(1)\n", None),
    "parameter shadows the import": ("from p.a import Pool\ndef f(Pool):\n    Pool.conns.append(1)\n", None),
    "global rebound to an int": ("from p.a import Pool\nx = Pool()\ndef f():\n    global x\n    x = 5\n    x.conns.append(1)\n", None),
    "star import is not guessed": ("from p.a import *\ndef f(x: Pool):\n    x.conns.append(1)\n", None),
    "lambda parameter": ("from p.a import Pool\nf = lambda x: x.conns.append(1)\n", None),
    "TypeVar bound": ("from typing import TypeVar\nfrom p.a import Pool\nT = TypeVar('T', bound=Pool)\ndef f(x: T):\n    x.conns.append(1)\n", None),
    "subclass is an insider": ("from p.a import Pool\nclass Sub(Pool):\n    def m(self):\n        self.conns.append(1)\n", None),
}


@pytest.mark.parametrize("name", list(RECEIVERS))
def test_receiver_typing(project: Run, name: str) -> None:
    body, leaked = RECEIVERS[name]
    found = project({**LIB, "p/b.py": body}, lints=["field-leak"])
    assert keys(found) == ([] if leaked is None else [f"field-leak:{leaked}"])


def test_attribute_write_counts_as_a_leak(project: Run) -> None:
    (f,) = project({**LIB, "p/b.py": "from p.a import Pool\ndef f(x: Pool):\n    x.n += 1\n"}, lints=["field-leak"])
    assert f.key == "field-leak:p.a.Pool.n"


def test_namespace_subpackage_gets_its_full_name(tmp_path: Path) -> None:
    write_files(tmp_path, {"p/__init__.py": "", "p/x/a.py": "", "p/x/y/b.py": "", "scripts/s.py": "", "top.py": ""})
    names = {s.rel: s.module for s in find_sources(tmp_path, Config())}
    assert names == {"p/__init__.py": "p", "p/x/a.py": "p.x.a", "p/x/y/b.py": "p.x.y.b", "scripts/s.py": "s", "top.py": "top"}


def test_src_layout_and_package_root(tmp_path: Path) -> None:
    write_files(tmp_path, {"src/pkg/__init__.py": "", "src/pkg/m.py": ""})
    assert module_name(tmp_path / "src/pkg/m.py", tmp_path) == ("pkg.m", False)
    assert module_name(tmp_path / "src/pkg/__init__.py", tmp_path) == ("pkg", True)
    # The project root itself is a package: it is part of the name, as before.
    write_files(tmp_path / "r", {"__init__.py": "", "q/a.py": ""})
    assert module_name(tmp_path / "r/q/a.py", tmp_path / "r") == ("r.q.a", False)


def test_cycle_through_directories_without_init(project: Run) -> None:
    found = project({
        "p/__init__.py": "",
        "p/x/a.py": "from p.y import b\n",
        "p/y/b.py": "from p.x import a\n",
    }, lints=["module-cycle"])
    assert keys(found) == ["module-cycle:p.x|p.y"]


def test_same_file_name_in_two_namespace_dirs_is_not_dropped(tmp_path: Path) -> None:
    write_files(tmp_path, {"p/__init__.py": "", "p/x/util.py": "", "p/y/util.py": ""})
    assert sorted(s.module for s in find_sources(tmp_path, Config()) if s.module != "p") == ["p.x.util", "p.y.util"]


@pytest.mark.parametrize("files", [
    {"p/__init__.py": "", "p/a.py": "from ... import z\n"},                 # relative import past the top
    {"p/__init__.py": "", "p/a.py": "import p.a\n"},                         # imports itself
    {"p/__init__.py": "", "p/a.py": "x = 1\x00\n"},                          # NUL byte
    {"p/__init__.py": "", "my-dir/a.py": "import p\n"},                      # not an importable name
    {},                                                                     # nothing at all
])
def test_odd_inputs_do_not_crash(project: Run, files: dict[str, str]) -> None:
    assert project(files) == []


def test_import_forms_all_close_a_cycle(project: Run) -> None:
    forms = [
        ("from . import b\n", "from . import a\n"),
        ("def f():\n    import PKG.b\n", "import PKG.a\n"),
        ("try:\n    import PKG.b\nexcept ImportError:\n    pass\n", "import PKG.a\n"),
    ]
    files: dict[str, str] = {}
    for i, (a, b) in enumerate(forms):
        files |= {f"c{i}/__init__.py": "", f"c{i}/a.py": a.replace("PKG", f"c{i}"), f"c{i}/b.py": b.replace("PKG", f"c{i}")}
    found = project(files, lints=["module-cycle"])
    assert keys(found) == [f"module-cycle:c{i}.a|c{i}.b" for i in range(len(forms))]
