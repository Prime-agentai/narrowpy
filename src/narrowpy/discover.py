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


@dataclass
class Config:
    exclude: list[str] = field(default_factory=list)
    include_tests: bool = False
    max_interface: int = HELD_IN_MIND
    lints: list[str] = field(default_factory=lambda: list(LINTS))
    baseline: str = ".narrowpy-baseline.json"

    @classmethod
    def load(cls, root: Path) -> Config:
        cfg = cls()
        pyproject = root / "pyproject.toml"
        if not pyproject.is_file():
            return cfg
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return cfg
        section = data.get("tool", {}).get("narrowpy", {})
        if not isinstance(section, dict):
            return cfg
        if isinstance(section.get("exclude"), list):
            cfg.exclude = [str(x) for x in section["exclude"]]
        if isinstance(section.get("include-tests"), bool):
            cfg.include_tests = section["include-tests"]
        if isinstance(section.get("max-interface"), int):
            cfg.max_interface = max(1, section["max-interface"])
        if isinstance(section.get("lints"), list):
            cfg.lints = [str(x) for x in section["lints"] if str(x) in LINTS]
        if isinstance(section.get("baseline"), str):
            cfg.baseline = section["baseline"]
        return cfg


@dataclass(frozen=True)
class SourceFile:
    path: Path
    rel: str  # posix, relative to the project root
    module: str
    is_package: bool


def _is_test_file(name: str) -> bool:
    return name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py")


def module_name(path: Path, root: Path) -> tuple[str, bool]:
    """Dotted module name of ``path``: climb while the parent is a package."""
    is_package = path.name == "__init__.py"
    parts: list[str] = [] if is_package else [path.stem]
    d = path.parent
    while (d / "__init__.py").is_file() and d != root.parent:
        parts.insert(0, d.name)
        if d == root:
            break
        d = d.parent
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
