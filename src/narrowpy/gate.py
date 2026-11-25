"""The ratchet: compare findings against a baseline file or a git revision.

An agent may leave old problems alone, but it may not add one or make one
bigger. A finding is *new* when its key is not in the reference, *worse* when
its metric grew, and *better* or *gone* when it shrank or disappeared.
"""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .discover import Config
from .findings import Finding
from .lints import analyze

BASELINE_VERSION = 1


class GateError(Exception):
    pass


@dataclass
class Comparison:
    new: list[Finding] = field(default_factory=list)
    worse: list[tuple[Finding, int]] = field(default_factory=list)
    unchanged: list[Finding] = field(default_factory=list)
    better: list[tuple[Finding, int]] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)

    @property
    def failing(self) -> list[Finding]:
        return self.new + [f for f, _ in self.worse]

    def labels(self) -> dict[str, str]:
        out = {f.key: "[new]" for f in self.new}
        out.update({f.key: f"[worse: {old} → {f.metric}]" for f, old in self.worse})
        return out

    def fingerprint(self) -> str:
        items = sorted(f"{f.key}={f.metric}" for f in self.failing)
        return hashlib.sha256("\n".join(items).encode()).hexdigest()[:16]


def compare(current: list[Finding], reference: dict[str, int]) -> Comparison:
    c = Comparison()
    keys = set()
    for f in current:
        keys.add(f.key)
        old = reference.get(f.key)
        if old is None:
            c.new.append(f)
        elif f.metric > old:
            c.worse.append((f, old))
        elif f.metric < old:
            c.better.append((f, old))
        else:
            c.unchanged.append(f)
    c.gone = sorted(k for k in reference if k not in keys)
    return c


# -- baseline file ------------------------------------------------------------------


def write_baseline(path: Path, findings: list[Finding]) -> None:
    body = {
        "tool": "narrowpy",
        "version": BASELINE_VERSION,
        "written_by": __version__,
        "findings": {f.key: {"metric": f.metric, "message": f.message} for f in sorted(findings, key=lambda f: f.key)},
    }
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_baseline(text: str, where: str) -> dict[str, int]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GateError(f"{where} is not valid JSON: {exc}") from None
    if not isinstance(data, dict) or data.get("tool") != "narrowpy" or not isinstance(data.get("findings"), dict):
        raise GateError(f"{where} is not a narrowpy baseline")
    out: dict[str, int] = {}
    for k, v in data["findings"].items():
        metric = v.get("metric") if isinstance(v, dict) else v
        if isinstance(metric, int):
            out[str(k)] = metric
    return out


def read_baseline(path: Path) -> dict[str, int]:
    return parse_baseline(path.read_text(encoding="utf-8"), str(path))


# -- git --------------------------------------------------------------------------


def git(root: Path, *args: str, binary: bool = False) -> Any:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GateError(f"git {' '.join(args)}: {exc}") from None
    if proc.returncode != 0:
        msg = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise GateError(f"git {' '.join(args)}: {msg[-1] if msg else 'failed'}")
    return proc.stdout if binary else proc.stdout.decode("utf-8", "replace").strip()


def repo_paths(root: Path) -> tuple[Path, str]:
    """The repository top and ``root``'s path inside it (posix, '' at the top)."""
    top = Path(git(root, "rev-parse", "--show-toplevel"))
    prefix = git(root, "rev-parse", "--show-prefix").rstrip("/")
    return top, prefix


def resolve_rev(root: Path, rev: str) -> str:
    return str(git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"))


def show_file(root: Path, rev: str, rel: str) -> str | None:
    """A file's content at ``rev`` (``rel`` relative to ``root``), or None if absent."""
    _top, prefix = repo_paths(root)
    path = f"{prefix}/{rel}" if prefix else rel
    try:
        return str(git(root, "show", f"{rev}:{path}"))
    except GateError:
        return None


def _extract(root: Path, rev: str, dest: Path) -> Path:
    top, prefix = repo_paths(root)
    args = ["archive", "--format=tar", rev]
    if prefix:
        args.append(prefix)
    data = git(top, *args, binary=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        members = [m for m in tf.getmembers() if m.isfile() and (m.name.endswith(".py") or m.name.endswith("pyproject.toml"))]
        for m in members:
            target = (dest / m.name).resolve()
            if not str(target).startswith(str(dest.resolve())):
                continue  # never write outside the scratch directory
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(m)
            if src is not None:
                target.write_bytes(src.read())
    return dest / prefix if prefix else dest


def _cache_dir(root: Path) -> Path | None:
    try:
        gitdir = Path(git(root, "rev-parse", "--absolute-git-dir"))
    except GateError:
        return None
    d = gitdir / "narrowpy" / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cfg_hash(cfg: Config) -> str:
    raw = json.dumps([sorted(cfg.exclude), cfg.include_tests, cfg.max_interface, sorted(cfg.lints), __version__])
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def findings_at(root: Path, rev: str, cfg: Config) -> dict[str, int]:
    """Analyse the tree at ``rev`` with today's config; cached per commit."""
    sha = resolve_rev(root, rev)
    if not sha:
        raise GateError(f"unknown revision: {rev}")
    cache = _cache_dir(root)
    _top, prefix = repo_paths(root)
    cache_file = cache / f"{sha}-{hashlib.sha256(prefix.encode()).hexdigest()[:8]}-{_cfg_hash(cfg)}.json" if cache else None
    if cache_file is not None and cache_file.is_file():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            return {str(k): int(v) for k, v in data.items()}
        except (ValueError, OSError):
            pass
    with tempfile.TemporaryDirectory(prefix="narrowpy-base-") as tmp:
        base_root = _extract(root, sha, Path(tmp))
        base_root.mkdir(parents=True, exist_ok=True)
        result = {f.key: f.metric for f in analyze(base_root, cfg).findings}
    if cache_file is not None:
        try:
            cache_file.write_text(json.dumps(result), encoding="utf-8")
        except OSError:
            pass
    return result
