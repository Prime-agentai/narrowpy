from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from narrowpy.discover import Config
from narrowpy.findings import Finding
from narrowpy.lints import analyze

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
}


def git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=GIT_ENV, check=True)
    return out.stdout.strip()


def commit_all(root: Path, msg: str = "c") -> str:
    git(root, "add", "-A")
    git(root, "commit", "-qm", msg, "--allow-empty")
    return git(root, "rev-parse", "HEAD")


def write_files(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body.lstrip("\n"), encoding="utf-8")
    return root


@pytest.fixture
def project(tmp_path: Path) -> Callable[[dict[str, str]], list[Finding]]:
    def run(files: dict[str, str], **cfg: object) -> list[Finding]:
        write_files(tmp_path, files)
        c = Config()
        for k, v in cfg.items():
            setattr(c, k, v)
        return analyze(tmp_path, c).findings
    return run


def keys(findings: list[Finding]) -> list[str]:
    return sorted(f.key for f in findings)
