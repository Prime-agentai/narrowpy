"""Output formats: what people, scripts, code scanning and GitHub Actions read."""

from __future__ import annotations

import json

import pytest

from narrowpy import render
from narrowpy.findings import Finding, Site

F = Finding(lint="field-leak", key="field-leak:p.A.x", message="100% of `A.x` leaks\nsecond line",
            site=Site("p/a.py", 0), metric=2, notes=["note one"], picture=["row"])
G = Finding(lint="module-cycle", key="module-cycle:a|b", message="cycle", site=None, metric=2)


def test_text_is_plain_without_color_and_has_hints_once_per_lint() -> None:
    out = render.text([F, F], color=False)
    assert "\033" not in out
    assert out.count("error[field-leak]") == 2 and out.count("  field-leak: ") == 1
    assert "  --> p/a.py:0" in out and "  = note one" in out and "    row" in out


def test_text_labels_and_no_hints() -> None:
    out = render.text([F], label={F.key: "(worse)"}, hints=False)
    assert "error[field-leak] (worse):" in out and "how to fix" not in out


def test_text_color_uses_ansi() -> None:
    assert "\033[1;31m" in render.text([F], color=True)


def test_no_findings_prints_nothing() -> None:
    assert render.text([], color=False) == ""


def test_color_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    class Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert render.wants_color(Tty()) is True  # type: ignore[arg-type]
    monkeypatch.setenv("NO_COLOR", "1")
    assert render.wants_color(Tty()) is False  # type: ignore[arg-type]
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert render.wants_color(object()) is True  # type: ignore[arg-type]


def test_json_carries_extra_fields_and_null_site() -> None:
    body = json.loads(render.as_json([G], {"mode": "all findings"}))
    assert body["mode"] == "all findings" and body["findings"][0]["file"] is None


def test_sarif_has_every_rule_and_clamps_line_zero() -> None:
    doc = json.loads(render.sarif([F, G]))
    run = doc["runs"][0]
    assert [r["id"] for r in run["tool"]["driver"]["rules"]] == ["module-cycle", "tangle", "field-leak", "shallow-module"]
    located, unlocated = run["results"]
    assert located["locations"][0]["physicalLocation"]["region"]["startLine"] == 1
    assert "locations" not in unlocated and located["partialFingerprints"]["narrowpyKey"] == F.key


def test_github_annotations_escape_percent_and_newlines() -> None:
    first, second = render.github([F, G]).split("\n")  # one annotation per line: newlines inside are escaped
    assert first.startswith("::error file=p/a.py,line=1,title=narrowpy field-leak::")
    assert "100%25" in first and "%0Asecond line" in first and "%0Anote one" in first
    assert second == "::error title=narrowpy module-cycle::cycle"
