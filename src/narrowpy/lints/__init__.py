"""Run the lints over a project."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from ..discover import Config
from ..facts import collect
from ..findings import Finding
from ..project import Project
from . import cycles, leaks, shallow, tangles


@dataclass
class Analysis:
    root: Path
    findings: list[Finding]
    modules: int
    classes: int
    functions: int
    seconds: float
    parse_errors: list[str] = field(default_factory=list)


def analyze(root: Path, cfg: Config | None = None) -> Analysis:
    t0 = time.perf_counter()
    cfg = cfg or Config.load(root)
    p = Project(root, cfg)
    facts, typer = collect(p)
    out: list[Finding] = []
    if "module-cycle" in cfg.lints:
        out += cycles.check(p)
    if "tangle" in cfg.lints:
        out += tangles.check(p, facts, cfg.max_interface)
    if "field-leak" in cfg.lints:
        out += leaks.check(p, facts, typer)
    if "shallow-module" in cfg.lints:
        out += shallow.check(p, facts, cfg.max_interface)
    order = {name: i for i, name in enumerate(("module-cycle", "tangle", "field-leak", "shallow-module"))}
    out.sort(key=lambda f: (order[f.lint], f.site.file if f.site else "", f.site.line if f.site else 0, f.key))
    return Analysis(
        root=p.root,
        findings=out,
        modules=len(p.modules),
        classes=len(p.classes),
        functions=len(p.functions) + sum(len(c.methods) for c in p.classes.values()),
        seconds=time.perf_counter() - t0,
        parse_errors=p.parse_errors,
    )
