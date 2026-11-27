"""Find the Python modules of a project and read its ``[tool.narrowpy]`` config."""

from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .findings import HELD_IN_MIND, LINTS

#: Directories that never hold project code.
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".env", ".tox", ".nox", "build", "dist",
    "node_modules", "__pycache__", "site-packages", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".eggs",
}
#: Directories whose code is a client of the project, not part of it.
OUTSIDER_DIRS = {"tests", "test", "docs", "examples", "example", "benchmarks", "benchmark"}


#: Keys `[tool.narrowpy]` understands. Anything else is reported, so a typo cannot quietly turn a check off.
CONFIG_KEYS = ("exclude", "include-tests", "max-interface", "lints", "baseline")


@dataclass
class Config:
    exclude: list[str] = field(default_factory=list)
    include_tests: bool = False
    max_interface: int = HELD_IN_MIND
    lints: list[str] = field(default_factory=lambda: list(LINTS))
    baseline: str = ".narrowpy-baseline.json"
    #: Things wrong with the config file. The defaults stay on for whatever could not be read,
    #: so a mistake makes narrowpy check more, never less.
    problems: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, root: Path) -> Config:
        cfg = cls()
        pyproject = root / "pyproject.toml"
        if not pyproject.is_file():
            return cfg
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            cfg.problems.append(f"pyproject.toml could not be read ({exc}); [tool.narrowpy] settings are ignored")
            return cfg
        section = data.get("tool", {}).get("narrowpy", {})
        if not isinstance(section, dict):
            cfg.problems.append("[tool.narrowpy] must be a table; settings are ignored")
            return cfg
        cfg._apply(section)
        return cfg

    def override(self, *, exclude: list[str] | None = None, include_tests: bool = False,
                 max_interface: int | None = None, lints: list[str] | None = None) -> None:
        """Command-line flags win over the file: globs add, the rest replace."""
        if exclude:
            self.exclude += exclude
        if include_tests:
            self.include_tests = True
        if max_interface:
            self.max_interface = max_interface
        if lints:
            self.lints = [x for x in lints if x in LINTS]

    def _apply(self, section: dict[str, object]) -> None:
        for key in section:
            if key not in CONFIG_KEYS:
                self.problems.append(f"unknown setting `{key}` in [tool.narrowpy] (known: {', '.join(CONFIG_KEYS)})")
        exclude = section.get("exclude")
        if isinstance(exclude, list):
            self.exclude = [str(x) for x in exclude]
        elif exclude is not None:
            self.problems.append("`exclude` must be a list of globs")
        include_tests = section.get("include-tests")
        if isinstance(include_tests, bool):
            self.include_tests = include_tests
        elif include_tests is not None:
            self.problems.append("`include-tests` must be true or false")
        max_interface = section.get("max-interface")
        if isinstance(max_interface, int) and not isinstance(max_interface, bool):
            self.max_interface = max(1, max_interface)
        elif max_interface is not None:
            self.problems.append("`max-interface` must be a whole number")
        lints = section.get("lints")
        if isinstance(lints, list):
            names = [str(x) for x in lints]
            unknown = [x for x in names if x not in LINTS]
            if unknown:
                self.problems.append(f"unknown lint {', '.join(f'`{x}`' for x in unknown)} in `lints` (known: {', '.join(LINTS)})")
            valid = [x for x in names if x in LINTS]
            if valid:
                self.lints = valid
            elif not names:
                self.lints = []
                self.problems.append("`lints` is empty, so no check runs")
            else:
                self.problems.append("none of the `lints` names is known; running all lints")
        elif lints is not None:
            self.problems.append("`lints` must be a list of lint names")
        baseline = section.get("baseline")
        if isinstance(baseline, str):
            self.baseline = baseline
        elif baseline is not None:
            self.problems.append("`baseline` must be a file name")


@dataclass(frozen=True)
class SourceFile:
    path: Path
    rel: str  # posix, relative to the project root
    module: str
    is_package: bool


def _is_test_file(name: str) -> bool:
    return name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py")


def module_name(path: Path, root: Path) -> tuple[str, bool]:
    """Dotted module name of ``path``.

    Climb through the parent directories and name the module from the highest
    one that has an ``__init__.py``. A directory without one *below* a regular
    package is a namespace package (PEP 420) and is part of the name, so
    ``p/x/a.py`` under ``p/__init__.py`` is ``p.x.a``, not ``a``. With no
    ``__init__.py`` anywhere above, the file is a top-level script or module.
    """
    is_package = path.name == "__init__.py"
    chain: list[Path] = []  # directories from the file's own up to the root (or the first one past it)
    d = path.parent
    while d != root.parent and d != d.parent:
        chain.append(d)
        if d == root:
            break
        d = d.parent
    top = max((i for i, c in enumerate(chain) if (c / "__init__.py").is_file()), default=-1)
    parts = [c.name for c in reversed(chain[: top + 1])]
    if not is_package:
        parts.append(path.stem)
    return ".".join(parts), is_package


def find_sources(root: Path, cfg: Config) -> list[SourceFile]:
    root = root.resolve()
    out: list[SourceFile] = []
    seen: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        rel_dir = d.relative_to(root).as_posix()
        keep = []
        for name in sorted(dirnames):
            if name in SKIP_DIRS or name.startswith(".") or name.endswith(".egg-info"):
                continue
            if not cfg.include_tests and name in OUTSIDER_DIRS:
                continue
            sub = f"{rel_dir}/{name}" if rel_dir != "." else name
            if any(fnmatch.fnmatch(sub, pat) or fnmatch.fnmatch(sub + "/", pat) for pat in cfg.exclude):
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            if not name.endswith(".py"):
                continue
            if not cfg.include_tests and _is_test_file(name):
                continue
            path = d / name
            rel = path.relative_to(root).as_posix()
            if any(fnmatch.fnmatch(rel, pat) for pat in cfg.exclude):
                continue
            mod, is_pkg = module_name(path, root)
            if not mod or mod in seen:
                continue  # a second file claiming the same name (e.g. two scripts) is skipped
            seen.add(mod)
            out.append(SourceFile(path=path, rel=rel, module=mod, is_package=is_pkg))
    return out
