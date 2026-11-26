"""Claude Code integration: a Stop hook that will not let an agent finish while
its changes made the structure worse, and the AGENTS.md lines that say why.

* ``SessionStart`` remembers the commit the session started from, so an agent
  that commits mid-session is still compared against where it began.
* ``Stop`` compares the working tree with that commit (or HEAD). New or worse
  findings block, with the findings as the reason. Changing the baseline file
  or the ``[tool.narrowpy]`` settings during the session blocks too: only a
  person moves the gate. The same unchanged findings block once; after that the
  agent may stop and the user is told what is left.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import render
from .discover import Config
from .gate import GateError, compare, findings_at, git, show_file
from .lints import analyze

SETTINGS_REL = (".claude", "settings.json")

AGENTS_MD = """\
## Structure (narrowpy)

- Run `narrowpy check` before you say you are done. It fails on structural problems you added or made worse.
- Fix what it reports from first principles. Do not do mechanical moves to make it quiet:
  - `module-cycle`: decide which module is the lower layer and make every import point down.
    Moving an import into a function or under `TYPE_CHECKING` hides a cycle; it does not break it.
  - `field-leak`: ask why outside code works on the field at all. Give the class a method that does
    the whole job, and fix all the places together.
  - `shallow-module` and `tangle`: what is the narrow interface? Hide the rest behind it.
  - Draw the before and after structure for the user when the fix moves code between modules.
- Never edit `.narrowpy-baseline.json` or the `[tool.narrowpy]` settings. Only a person runs `narrowpy baseline`.
"""


SOURCE = "git+https://github.com/Prime-agentai/narrowpy"


def _ephemeral(path: str) -> bool:
    """Inside a throwaway ``uvx`` / ``uv tool run`` environment that may be gone tomorrow."""
    # uv's cache layout, wherever the cache is (~/.cache/uv, UV_CACHE_DIR, a --no-cache temp dir).
    return re.search(r"[/\\]archive-v\d+[/\\]", path) is not None


def command_prefix() -> str:
    """How a hook should start narrowpy later, from a fresh shell."""
    exe = shutil.which("narrowpy")
    if exe and not _ephemeral(exe):
        return "narrowpy"
    if not _ephemeral(sys.executable) and not exe:
        return f'"{sys.executable}" -m narrowpy'
    uv = os.environ.get("UV") or shutil.which("uv")
    return f'"{uv}" tool run --from {SOURCE} narrowpy' if uv else f"uvx --from {SOURCE} narrowpy"


def install(root: Path, prefix: str | None = None) -> tuple[Path, bool]:
    path = root.joinpath(*SETTINGS_REL)
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise GateError(f"{path} is not valid JSON ({exc}); not touching it") from None
        if isinstance(loaded, dict):
            data = loaded
    hooks = data.setdefault("hooks", {})
    prefix = prefix or command_prefix()
    changed = False
    for event, sub in (("SessionStart", "session-start"), ("Stop", "stop")):
        entries = hooks.setdefault(event, [])
        present = any("narrowpy" in h.get("command", "") and f"hook {sub}" in h.get("command", "")
                      for e in entries if isinstance(e, dict) for h in e.get("hooks", []) if isinstance(h, dict))
        if not present:
            entries.append({"hooks": [{"type": "command", "command": f"{prefix} hook {sub}", "timeout": 120}]})
            changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path, changed


def _read_event() -> dict[str, Any]:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def _state_dir(root: Path) -> Path | None:
    try:
        gitdir = Path(git(root, "rev-parse", "--absolute-git-dir"))
    except GateError:
        return None
    d = gitdir / "narrowpy" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_id(session: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session)[:120] or "default"


def session_start(event: dict[str, Any] | None = None) -> int:
    event = event if event is not None else _read_event()
    root = Path(event.get("cwd") or ".")
    state = _state_dir(root)
    if state is None:
        return 0
    try:
        head = git(root, "rev-parse", "HEAD")
    except GateError:
        return 0  # no commits yet
    (state / f"{_safe_id(str(event.get('session_id', 'default')))}.base").write_text(str(head), encoding="utf-8")
    return 0


def _tool_section(text: str | None) -> Any:
    if text is None:
        return None
    try:
        return tomllib.loads(text).get("tool", {}).get("narrowpy")
    except tomllib.TOMLDecodeError:
        return "<unparseable>"


def _content(text: str | None) -> Any:
    """What a baseline says, ignoring line endings and formatting (CRLF checkouts on Windows)."""
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return "\n".join(line.rstrip() for line in text.strip().splitlines())


def tampering(root: Path, base: str, cfg: Config) -> list[str]:
    out = []
    baseline = root / cfg.baseline
    before = show_file(root, base, cfg.baseline)
    now = baseline.read_text(encoding="utf-8") if baseline.is_file() else None
    if _content(before) != _content(now):
        out.append(f"`{cfg.baseline}` changed since the session started")
    py = root / "pyproject.toml"
    if _tool_section(show_file(root, base, "pyproject.toml")) != _tool_section(py.read_text(encoding="utf-8") if py.is_file() else None):
        out.append("the `[tool.narrowpy]` settings in pyproject.toml changed since the session started")
    return out


def stop(event: dict[str, Any] | None = None) -> int:
    event = event if event is not None else _read_event()
    root = Path(event.get("cwd") or ".").resolve()
    session = _safe_id(str(event.get("session_id", "default")))
    state = _state_dir(root)
    if state is None:
        return 0  # not a git repository: nothing to compare against
    base_file = state / f"{session}.base"
    base = base_file.read_text(encoding="utf-8").strip() if base_file.is_file() else "HEAD"
    cfg = Config.load(root)
    for problem in cfg.problems:
        print(f"narrowpy: config: {problem}", file=sys.stderr)
    try:
        reference = findings_at(root, base, cfg)
    except GateError:
        return 0  # e.g. a repository without commits
    tampered = tampering(root, base, cfg)
    current = analyze(root, cfg).findings
    cmp = compare(current, reference)
    if not cmp.failing and not tampered:
        return 0
    fp = cmp.fingerprint() + ("+tamper" if tampered else "")
    last_file = state / f"{session}.last"
    last = last_file.read_text(encoding="utf-8").strip() if last_file.is_file() else ""
    if event.get("stop_hook_active") and last == fp:
        n = len(cmp.failing)
        msg = f"narrowpy: {n} structural regression{'s' if n != 1 else ''} left after one block; the agent was allowed to stop."
        if tampered:
            msg += " The gate's own settings were changed in this session: " + "; ".join(tampered) + "."
        print(json.dumps({"systemMessage": msg}))
        return 0
    last_file.write_text(fp, encoding="utf-8")
    parts = []
    if tampered:
        parts.append("The structure gate's own settings were changed in this session: " + "; ".join(tampered)
                     + ". Restore them. Only a person moves the gate (`narrowpy baseline`).")
    if cmp.failing:
        parts.append(f"narrowpy: your changes since {base[:12]} made the structure worse "
                     f"({len(cmp.new)} new, {len(cmp.worse)} worse). Fix these before you stop:\n")
        parts.append(render.text(cmp.failing, label=cmp.labels()))
    print(json.dumps({"decision": "block", "reason": "\n".join(parts)}))
    return 0
