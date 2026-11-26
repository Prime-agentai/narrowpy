"""A minimal MCP server over stdio, so an agent can run the lints as a tool.

Newline-delimited JSON-RPC 2.0, one tool: ``narrowpy_check``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

from . import __version__, render
from .discover import Config
from .gate import GateError, compare, findings_at, read_baseline
from .lints import analyze

PROTOCOL = "2025-06-18"

TOOL = {
    "name": "narrowpy_check",
    "title": "Check Python structure (deep modules, narrow interfaces)",
    "description": (
        "Finds module cycles (including ones hidden by function-local or TYPE_CHECKING imports), "
        "field leaks, tangles and shallow modules in a Python project. With `base` (a git revision) "
        "or when the project has a baseline file, reports only what is new or worse. Fix findings from "
        "first principles; never move code just to silence them."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Project root (default: the server's working directory)"},
            "base": {"type": "string", "description": "Git revision to compare against, e.g. HEAD or main"},
            "all": {"type": "boolean", "description": "Report every finding, ignoring baseline and base"},
        },
    },
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
}


def run_check(args: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
    root = Path(str(args.get("path") or ".")).resolve()
    if not root.is_dir():
        return f"not a directory: {root}", {"error": "not a directory"}, True
    cfg = Config.load(root)
    analysis = analyze(root, cfg)
    findings = analysis.findings
    labels: dict[str, str] = {}
    mode = "all"
    try:
        if not args.get("all"):
            ref: dict[str, int] | None = None
            if args.get("base"):
                ref, mode = findings_at(root, str(args["base"]), cfg), f"since {args['base']}"
            elif (root / cfg.baseline).is_file():
                ref, mode = read_baseline(root / cfg.baseline), "against the baseline"
            if ref is not None:
                cmp = compare(findings, ref)
                findings, labels = cmp.failing, cmp.labels()
    except GateError as exc:
        return f"narrowpy: {exc}", {"error": str(exc)}, True
    head = (f"narrowpy: {len(findings)} finding{'s' if len(findings) != 1 else ''} ({mode}) in "
            f"{analysis.modules} modules" + "".join(f"\nnarrowpy: config: {p}" for p in cfg.problems))
    body = render.text(findings, label=labels) if findings else "No structural problems found."
    data = {"mode": mode, "modules": analysis.modules, "findings": [f.to_json() for f in findings]}
    return head + "\n\n" + body, data, False


def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    method = msg.get("method")
    mid = msg.get("id")
    if mid is None:
        return None  # a notification
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "narrowpy", "version": __version__},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        if params.get("name") != TOOL["name"]:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool: {params.get('name')}"}}
        text, data, is_error = run_check(params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "structuredContent": data, "isError": is_error}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    inp, out = stdin or sys.stdin, stdout or sys.stdout
    for line in inp:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply: dict[str, Any] | None = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            try:
                reply = handle(msg) if isinstance(msg, dict) else None
            except Exception as exc:  # a tool crash must not kill the server
                reply = {"jsonrpc": "2.0", "id": msg.get("id") if isinstance(msg, dict) else None,
                         "error": {"code": -32603, "message": f"{exc.__class__.__name__}: {exc}"}}
        if reply is not None:
            out.write(json.dumps(reply) + "\n")
            out.flush()
    return 0
