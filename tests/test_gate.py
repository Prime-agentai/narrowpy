from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from conftest import commit_all, git, write_files
from narrowpy import hooks, mcp
from narrowpy.cli import main
from narrowpy.discover import Config
from narrowpy.findings import Finding
from narrowpy.gate import GateError, compare, findings_at, read_baseline, write_baseline
import sloppy

CLEAN = {
    "app/__init__.py": "",
    "app/store.py": "def save(x: int) -> int:\n    return x\n",
    "app/cli.py": "from app import store\ndef main() -> None:\n    print(store.save(3))\n",
}
# What an agent typically does to "fix" the ImportError it caused: a function-local import.
REGRESSION = {"app/store.py": "def save(x: int) -> int:\n    from app.cli import main\n    return x\n"}


def f(key: str, metric: int = 1) -> Finding:
    return Finding("field-leak", key, key, None, metric)


def test_compare_classifies() -> None:
    c = compare([f("a"), f("b", 3), f("c", 1), f("d", 2)], {"b": 2, "c": 2, "d": 2, "gone": 1})
    assert [x.key for x in c.new] == ["a"]
    assert [(x.key, old) for x, old in c.worse] == [("b", 2)]
    assert [(x.key, old) for x, old in c.better] == [("c", 2)]
    assert [x.key for x in c.unchanged] == ["d"]
    assert c.gone == ["gone"]
    assert [x.key for x in c.failing] == ["a", "b"]
    assert c.labels() == {"a": "[new]", "b": "[worse: 2 → 3]"}


def test_baseline_roundtrip_and_errors(tmp_path: Path) -> None:
    p = tmp_path / "b.json"
    write_baseline(p, [f("x", 4), f("y")])
    assert read_baseline(p) == {"x": 4, "y": 1}
    p.write_text("{}")
    with pytest.raises(GateError):
        read_baseline(p)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    write_files(tmp_path, CLEAN)
    commit_all(tmp_path)
    return tmp_path


def test_findings_at_a_revision_and_cache(repo: Path) -> None:
    write_files(repo, REGRESSION)
    commit_all(repo, "regress")
    assert findings_at(repo, "HEAD~1", Config()) == {}
    assert list(findings_at(repo, "HEAD", Config())) == ["module-cycle:app.cli|app.store"]
    cache = list((repo / ".git" / "narrowpy" / "cache").iterdir())
    assert len(cache) == 2
    with pytest.raises(GateError):
        findings_at(repo, "no-such-rev", Config())


def test_findings_at_in_a_subdirectory(tmp_path: Path) -> None:
    git(tmp_path, "init", "-q", "-b", "main")
    sub = tmp_path / "services" / "api"
    write_files(sub, {**CLEAN, **REGRESSION})
    commit_all(tmp_path)
    assert list(findings_at(sub, "HEAD", Config())) == ["module-cycle:app.cli|app.store"]


def test_check_base_reports_only_the_regression(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sloppy.write(repo, {k: v for k, v in sloppy.FILES.items() if "queue" in k or "policy" in k or k == "app/__init__.py"})
    commit_all(repo, "old leaks")
    write_files(repo, REGRESSION)
    assert main(["check", str(repo), "--base", "HEAD", "--format", "json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert [x["key"] for x in out["findings"]] == ["module-cycle:app.cli|app.store"]
    assert out["mode"] == "new or worse since HEAD"


def test_baseline_ratchet_flow(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(repo, REGRESSION)
    assert main(["check", str(repo)]) == 1
    assert main(["baseline", str(repo)]) == 0
    assert (repo / ".narrowpy-baseline.json").is_file()
    capsys.readouterr()
    assert main(["check", str(repo)]) == 0
    assert "1 known finding not shown" in capsys.readouterr().out
    # Making the known cycle bigger is a regression even though its modules are the same.
    write_files(repo, {"app/store.py": REGRESSION["app/store.py"] + "def two() -> None:\n    from app import cli\n"})
    assert main(["check", str(repo)]) == 1
    assert main(["check", str(repo), "--no-baseline"]) == 1
    write_files(repo, CLEAN)
    capsys.readouterr()
    assert main(["check", str(repo)]) == 0
    assert "run `narrowpy baseline` to lock in the gain" in capsys.readouterr().err


def test_output_formats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    sloppy.write(tmp_path)
    assert main([str(tmp_path), "--format", "sarif"]) == 1
    sarif = json.loads(capsys.readouterr().out)
    results = sarif["runs"][0]["results"]
    assert len(results) == 4 and results[0]["ruleId"] == "module-cycle"
    assert results[0]["partialFingerprints"]["narrowpyKey"] == "module-cycle:app.cli|app.store"
    assert main([str(tmp_path), "--format", "github"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("::error file=app/cli.py,line=2,title=narrowpy module-cycle::")
    assert main([str(tmp_path), "--lint", "field-leak", "--no-hints"]) == 1
    text = capsys.readouterr().out
    assert "error[field-leak]" in text and "module-cycle" not in text and "how to fix" not in text


def test_cli_misc(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", str(tmp_path / "missing")]) == 2
    assert main(["explain", "field-leak"]) == 0
    assert "Records" in capsys.readouterr().out
    assert main(["agents-md"]) == 0
    assert "narrowpy check" in capsys.readouterr().out
    (tmp_path / "x.py").write_text("x = 1\n")
    assert main([str(tmp_path)]) == 0
    assert "0 findings" in capsys.readouterr().out


# -- hooks ----------------------------------------------------------------------------


def run_stop(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], **event: object) -> dict[str, object] | None:
    capsys.readouterr()
    assert hooks.stop({"cwd": str(repo), "session_id": "s1", **event}) == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def test_install_is_idempotent_and_keeps_settings(tmp_path: Path) -> None:
    settings = tmp_path.joinpath(*hooks.SETTINGS_REL)
    settings.parent.mkdir()
    settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]}}))
    path, changed = hooks.install(tmp_path)
    assert changed and path == settings
    data = json.loads(settings.read_text())
    assert data["model"] == "x"
    assert [h["hooks"][0]["command"].endswith("hook stop") for h in data["hooks"]["Stop"]] == [False, True]
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"].endswith("hook session-start")
    assert hooks.install(tmp_path) == (settings, False)
    settings.write_text("{broken")
    with pytest.raises(GateError):
        hooks.install(tmp_path)


def test_hook_command_survives_an_ephemeral_uvx_run(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = "/home/u/.cache/uv/archive-v0/abc/bin"
    monkeypatch.setattr(hooks.shutil, "which", lambda name: f"{cache}/{name}" if name == "narrowpy" else None)
    monkeypatch.setattr(hooks.sys, "executable", f"{cache}/python")
    monkeypatch.setenv("UV", "/opt/uv")
    assert hooks.command_prefix() == '"/opt/uv" tool run --from git+https://github.com/Prime-agentai/narrowpy narrowpy'
    monkeypatch.delenv("UV")
    assert hooks.command_prefix().startswith("uvx --from git+")
    monkeypatch.setattr(hooks.shutil, "which", lambda name: "/usr/local/bin/narrowpy" if name == "narrowpy" else None)
    assert hooks.command_prefix() == "narrowpy"
    monkeypatch.setattr(hooks.shutil, "which", lambda name: None)
    monkeypatch.setattr(hooks.sys, "executable", "/usr/bin/python3")
    assert hooks.command_prefix() == '"/usr/bin/python3" -m narrowpy'


def test_install_with_explicit_command(tmp_path: Path) -> None:
    path, _ = hooks.install(tmp_path, "my-narrowpy")
    assert json.loads(path.read_text())["hooks"]["Stop"][0]["hooks"][0]["command"] == "my-narrowpy hook stop"


def test_stop_hook_flow(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    hooks.session_start({"cwd": str(repo), "session_id": "s1"})
    assert run_stop(repo, monkeypatch, capsys) is None  # nothing changed
    write_files(repo, REGRESSION)
    commit_all(repo, "agent commits mid-session")  # still compared with where the session began
    blocked = run_stop(repo, monkeypatch, capsys)
    assert blocked is not None and blocked["decision"] == "block"
    reason = str(blocked["reason"])
    assert "1 new, 0 worse" in reason and "[new]" in reason and "function-local" in reason
    # Told once; the same findings again let the agent stop, and the user is told.
    again = run_stop(repo, monkeypatch, capsys, stop_hook_active=True)
    assert again is not None and "decision" not in again and "1 structural regression left" in str(again["systemMessage"])
    write_files(repo, CLEAN)
    assert run_stop(repo, monkeypatch, capsys, stop_hook_active=True) is None


def test_stop_hook_blocks_edits_to_the_gate(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "pyproject.toml").write_text("[tool.narrowpy]\nmax-interface = 7\n")
    write_baseline(repo / ".narrowpy-baseline.json", [])
    commit_all(repo)
    hooks.session_start({"cwd": str(repo), "session_id": "s1"})
    write_baseline(repo / ".narrowpy-baseline.json", [f("x")])
    blocked = run_stop(repo, monkeypatch, capsys)
    assert blocked is not None and ".narrowpy-baseline.json` changed" in str(blocked["reason"])
    git(repo, "checkout", "--", ".narrowpy-baseline.json")
    (repo / "pyproject.toml").write_text("[tool.narrowpy]\nmax-interface = 50\n")
    blocked = run_stop(repo, monkeypatch, capsys)
    assert blocked is not None and "[tool.narrowpy]" in str(blocked["reason"])
    (repo / "pyproject.toml").write_text("[tool.narrowpy]\nmax-interface = 7\n\n[tool.other]\nx = 1\n")
    assert run_stop(repo, monkeypatch, capsys) is None  # other sections are not the gate


def test_stop_hook_outside_git_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sloppy.write(tmp_path)
    assert run_stop(tmp_path, monkeypatch, capsys) is None


# -- MCP ------------------------------------------------------------------------------


def test_mcp_server(repo: Path) -> None:
    write_files(repo, REGRESSION)
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "narrowpy_check", "arguments": {"path": str(repo), "base": "HEAD"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope"}},
        {"jsonrpc": "2.0", "id": 5, "method": "resources/list"},
    ]
    out = io.StringIO()
    mcp.serve(io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\nnot json\n"), out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, 3, 4, 5, None]
    assert replies[0]["result"]["serverInfo"]["name"] == "narrowpy"
    assert replies[1]["result"]["tools"][0]["name"] == "narrowpy_check"
    call = replies[2]["result"]
    assert call["isError"] is False and "since HEAD" in call["content"][0]["text"]
    assert [x["key"] for x in call["structuredContent"]["findings"]] == ["module-cycle:app.cli|app.store"]
    assert replies[3]["error"]["code"] == -32602
    assert replies[4]["error"]["code"] == -32601
    assert replies[5]["error"]["code"] == -32700
