"""Command line: ``narrowpy check | baseline | hook | agents-md | mcp | explain``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, hooks, mcp, render
from .discover import Config
from .findings import HINTS, LINTS
from .gate import GateError, compare, findings_at, read_baseline, write_baseline
from .lints import analyze

EXPLAIN = {
    "module-cycle": "Sibling modules that import each other in a circle, so there is no order to read them in. "
                    "Every import counts: function-local and TYPE_CHECKING imports are called out as hidden. "
                    "A package and the modules inside it make no circle.",
    "tangle": "Classes and functions that know each other in a circle, where the module graph cannot see it: "
              "more of them than a reader holds in mind, or a package tied to a module inside it.",
    "field-leak": "Code in another module that writes a class's field, indexes it, calls a method on it or "
                  "reaches through it. Reading a value is fine. Records (dataclasses, NamedTuples, enums, "
                  "exceptions, classes without behaviour) are exempt. Private (_name) fields leak on any use.",
    "shallow-module": "A module or package whose outside callers use more functions than a reader holds in mind, "
                      "at least as many as it hides, with one list (things, or one thing's functions) too long.",
}


def _config(args: argparse.Namespace, root: Path) -> Config:
    cfg = Config.load(root)
    if getattr(args, "exclude", None):
        cfg.exclude += args.exclude
    if getattr(args, "include_tests", False):
        cfg.include_tests = True
    if getattr(args, "max_interface", None):
        cfg.max_interface = args.max_interface
    if getattr(args, "lint", None):
        cfg.lints = [x for x in args.lint if x in LINTS]
    return cfg


def cmd_check(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"narrowpy: not a directory: {root}", file=sys.stderr)
        return 2
    cfg = _config(args, root)
    analysis = analyze(root, cfg)
    findings = analysis.findings
    labels: dict[str, str] = {}
    mode = "all findings"
    hidden = 0
    try:
        ref: dict[str, int] | None = None
        if args.base:
            ref, mode = findings_at(root, args.base, cfg), f"new or worse since {args.base}"
        elif not args.no_baseline:
            bl = Path(args.baseline) if args.baseline else root / cfg.baseline
            if bl.is_file():
                ref, mode = read_baseline(bl), f"new or worse than {bl.name}"
        if ref is not None:
            cmp = compare(findings, ref)
            findings, labels = cmp.failing, cmp.labels()
            hidden = len(cmp.unchanged) + len(cmp.better)
            if cmp.better or cmp.gone:
                gains = len(cmp.better) + len(cmp.gone)
                print(f"narrowpy: {gains} finding{'s' if gains != 1 else ''} improved or fixed"
                      + (" - run `narrowpy baseline` to lock in the gain" if not args.base else ""), file=sys.stderr)
    except GateError as exc:
        print(f"narrowpy: {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(render.as_json(findings, {"mode": mode, "modules": analysis.modules, "known": hidden}))
    elif args.format == "sarif":
        print(render.sarif(findings))
    elif args.format == "github":
        out = render.github(findings)
        if out:
            print(out)
    else:
        if findings:
            print(render.text(findings, color=render.wants_color(sys.stdout), label=labels, hints=not args.no_hints))
        summary = (f"narrowpy: {len(findings)} finding{'s' if len(findings) != 1 else ''} ({mode}) in "
                   f"{analysis.modules} modules, {analysis.classes} classes, {analysis.functions} functions "
                   f"[{analysis.seconds:.2f}s]")
        if hidden:
            summary += f"; {hidden} known finding{'s' if hidden != 1 else ''} not shown (--no-baseline to see them)"
        print(summary)
    for err in analysis.parse_errors:
        print(f"narrowpy: skipped unparseable {err}", file=sys.stderr)
    return 1 if findings else 0


def cmd_baseline(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    cfg = _config(args, root)
    findings = analyze(root, cfg).findings
    path = Path(args.output) if args.output else root / cfg.baseline
    write_baseline(path, findings)
    print(f"narrowpy: wrote {len(findings)} known finding{'s' if len(findings) != 1 else ''} to {path}")
    print("Commit it. From now on `narrowpy check` fails only on findings that are new or worse.")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    if args.action == "install":
        root = Path(args.path).resolve()
        try:
            path, changed = hooks.install(root)
        except GateError as exc:
            print(f"narrowpy: {exc}", file=sys.stderr)
            return 2
        print(f"narrowpy: {'added SessionStart + Stop hooks to' if changed else 'hooks already present in'} {path}")
        print("Claude Code now has to fix new structural problems before it can stop.")
        return 0
    if args.action == "session-start":
        return hooks.session_start()
    return hooks.stop()


def cmd_explain(args: argparse.Namespace) -> int:
    for lint in [args.lint] if args.lint else LINTS:
        print(f"{lint}\n  what: {EXPLAIN[lint]}\n  fix:  {HINTS[lint]}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="narrowpy", description="Deep modules, narrow interfaces: a structural linter and agent gate for Python.")
    p.add_argument("--version", action="version", version=f"narrowpy {__version__}")
    sub = p.add_subparsers(dest="cmd")

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("path", nargs="?", default=".", help="project root (default: .)")
        sp.add_argument("--exclude", action="append", metavar="GLOB", help="skip paths matching GLOB (repeatable)")
        sp.add_argument("--include-tests", action="store_true", help="analyse tests/, docs/, examples/ and test_*.py too")
        sp.add_argument("--max-interface", type=int, metavar="N", help="what a reader holds in mind (default 7)")
        sp.add_argument("--lint", action="append", choices=LINTS, help="run only this lint (repeatable)")

    c = sub.add_parser("check", help="report structural problems (exit 1 if any are new)")
    common(c)
    c.add_argument("--base", metavar="REV", help="report only what is new or worse than at git revision REV")
    c.add_argument("--baseline", metavar="FILE", help="baseline file (default .narrowpy-baseline.json)")
    c.add_argument("--no-baseline", action="store_true", help="report every finding")
    c.add_argument("--format", choices=["text", "json", "sarif", "github"], default="text")
    c.add_argument("--no-hints", action="store_true", help="do not print how to fix")
    c.set_defaults(func=cmd_check)

    b = sub.add_parser("baseline", help="record today's findings as known (the ratchet starts here)")
    common(b)
    b.add_argument("-o", "--output", metavar="FILE")
    b.set_defaults(func=cmd_baseline)

    h = sub.add_parser("hook", help="Claude Code hooks: install, or run as session-start / stop")
    h.add_argument("action", choices=["install", "session-start", "stop"])
    h.add_argument("path", nargs="?", default=".")
    h.set_defaults(func=cmd_hook)

    a = sub.add_parser("agents-md", help="print the lines to add to AGENTS.md / CLAUDE.md")
    a.set_defaults(func=lambda _a: (print(hooks.AGENTS_MD, end=""), 0)[1])

    m = sub.add_parser("mcp", help="run as an MCP server on stdio (tool: narrowpy_check)")
    m.set_defaults(func=lambda _a: mcp.serve())

    e = sub.add_parser("explain", help="what each lint means and how to fix it")
    e.add_argument("lint", nargs="?", choices=LINTS)
    e.set_defaults(func=cmd_explain)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    known = {"check", "baseline", "hook", "agents-md", "mcp", "explain", "-h", "--help", "--version"}
    if not argv or argv[0] not in known:
        argv.insert(0, "check")  # `narrowpy` and `narrowpy src/` mean check
    args = build_parser().parse_args(argv)
    try:
        rc: int = args.func(args)
    except KeyboardInterrupt:
        return 130
    return rc
