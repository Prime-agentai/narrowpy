"""A small app with one of each structural problem, written the way agents write them."""

from __future__ import annotations

import sys
from pathlib import Path

FILES: dict[str, str] = {
    "app/__init__.py": "",
    "app/queue.py": '''
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Job:
    name: str
    priority: int = 0


class RunQueue:
    def __init__(self, limit: int) -> None:
        self.items: list[Job] = []
        self.head = 0
        self.limit = limit
        self._seen: set[str] = set()

    def push(self, job: Job) -> None:
        if len(self.items) >= self.limit:
            raise OverflowError(job.name)
        self.items.append(job)
        self._seen.add(job.name)

    def pop(self) -> Job:
        job = self.items[self.head]
        self.head += 1
        return job
''',
    "app/policy.py": '''
from __future__ import annotations
from app.queue import Job, RunQueue


def admit(q: RunQueue, job: Job) -> bool:
    if job.name in q._seen:
        return False
    q.items.append(job)
    return True


def next_job(q: RunQueue) -> Job:
    job = q.items[q.head]
    q.head += 1
    return job
''',
    "app/report.py": '''
from __future__ import annotations
from app.queue import RunQueue


def backlog(q: RunQueue) -> int:
    return len(q.items) - q.head


def summary(q: RunQueue) -> str:
    return f"{q.limit} max"
''',
    "app/store.py": '''
from __future__ import annotations


def save(x: int) -> int:
    from app.cli import render
    return len(render(x))
''',
    "app/cli.py": '''
from __future__ import annotations
from app import store


def render(x: int) -> str:
    return str(x)


def main() -> None:
    print(store.save(3))
''',
}


def write(root: Path, files: dict[str, str] | None = None) -> Path:
    for rel, body in (files or FILES).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.lstrip("\n"), encoding="utf-8")
    return root


if __name__ == "__main__":
    write(Path(sys.argv[1]))
