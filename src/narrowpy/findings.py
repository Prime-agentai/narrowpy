"""What a lint reports, independent of how it is printed or compared."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

LINTS = ("module-cycle", "tangle", "field-leak", "shallow-module")

#: What a reader holds in mind at once (Miller, 1956). Interfaces and tangles
#: up to this size need no justification.
HELD_IN_MIND = 7


@dataclass(frozen=True, order=True)
class Site:
    """A place in the code: a repo-relative path and a 1-based line."""

    file: str
    line: int

    def __str__(self) -> str:
        return f"{self.file}:{self.line}"


@dataclass
class Finding:
    """One structural problem.

    ``key`` is stable across edits that do not change the structure (no line
    numbers in it), so a baseline can recognise the same finding later.
    ``metric`` says how big it is; a finding whose metric grows got worse.
    """

    lint: str
    key: str
    message: str
    site: Site | None
    metric: int
    notes: list[str] = field(default_factory=list)
    picture: list[str] = field(default_factory=list)
    hint: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "lint": self.lint,
            "key": self.key,
            "message": self.message,
            "file": self.site.file if self.site else None,
            "line": self.site.line if self.site else None,
            "metric": self.metric,
            "notes": self.notes,
            "hint": self.hint,
        }


HINTS = {
    "module-cycle": (
        "Decide which module is the lower layer and make every use point down. "
        "Moving an import into a function or under TYPE_CHECKING hides a cycle; it does not break it."
    ),
    "tangle": (
        "These things cannot be learned one at a time. Find the one that should not know the others "
        "(usually the lower-level one) and pass it what it needs instead."
    ),
    "field-leak": (
        "Ask why outsiders work on this field at all. Give the class a method that does the job, "
        "and fix all the places together rather than one leak at a time."
    ),
    "shallow-module": (
        "Callers have to learn too many functions to use this module. "
        "Hide most of them behind a few deeper ones that do a whole job each."
    ),
}
