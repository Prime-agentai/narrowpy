"""Print findings for people (text), programs (json), code scanning (sarif) and Actions (github)."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from typing import Any, TextIO

from . import __version__
from .findings import HINTS, LINTS, Finding


def _color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


def wants_color(stream: TextIO | None = None) -> bool:
    return _color(stream or sys.stdout)


def text(findings: Sequence[Finding], *, color: bool = False, label: dict[str, str] | None = None,
         hints: bool = True) -> str:
    c = color
    red = "\033[1;31m" if c else ""
    dim = "\033[2m" if c else ""
    bold = "\033[1m" if c else ""
    blue = "\033[34m" if c else ""
    off = "\033[0m" if c else ""
    lines: list[str] = []
    for f in findings:
        tag = f" {label[f.key]}" if label and f.key in label else ""
        lines.append(f"{red}error[{f.lint}]{off}{tag}: {bold}{f.message}{off}")
        if f.site:
            lines.append(f"  {blue}-->{off} {f.site}")
        for n in f.notes:
            lines.append(f"  {blue}={off} {n}")
        for row in f.picture:
            lines.append(f"    {dim}{row}{off}")
        lines.append("")
    if hints and findings:
        seen = [lint for lint in LINTS if any(f.lint == lint for f in findings)]
        lines.append(f"{bold}how to fix (from first principles, not by moving code until the lint goes quiet):{off}")
        for lint in seen:
            lines.append(f"  {lint}: {HINTS[lint]}")
        lines.append("")
    return "\n".join(lines)


def as_json(findings: Sequence[Finding], extra: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {"tool": "narrowpy", "version": __version__, "findings": [f.to_json() for f in findings]}
    if extra:
        body.update(extra)
    return json.dumps(body, indent=2)


def sarif(findings: Sequence[Finding]) -> str:
    rules = [{"id": lint, "shortDescription": {"text": lint}, "help": {"text": HINTS[lint]}} for lint in LINTS]
    results = []
    for f in findings:
        r: dict[str, Any] = {
            "ruleId": f.lint,
            "level": "error",
            "message": {"text": f.message + ("\n" + "\n".join(f.notes) if f.notes else "")},
            "partialFingerprints": {"narrowpyKey": f.key},
        }
        if f.site:
            r["locations"] = [{"physicalLocation": {"artifactLocation": {"uri": f.site.file},
                                                    "region": {"startLine": max(1, f.site.line)}}}]
        results.append(r)
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "narrowpy", "version": __version__,
                                      "informationUri": "https://github.com/Prime-agentai/narrowpy", "rules": rules}},
                  "results": results}],
    }
    return json.dumps(doc, indent=2)


def github(findings: Sequence[Finding]) -> str:
    def esc(s: str) -> str:
        return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    out = []
    for f in findings:
        loc = f"file={f.site.file},line={max(1, f.site.line)}," if f.site else ""
        body = f.message + ("\n" + "\n".join(f.notes) if f.notes else "")
        out.append(f"::error {loc}title=narrowpy {f.lint}::{esc(body)}")
    return "\n".join(out)
