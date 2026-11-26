"""Config parsing and source discovery: the settings that decide what narrowpy looks at."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import write_files
from narrowpy.cli import main
from narrowpy.discover import Config, find_sources, module_name
from narrowpy.findings import HELD_IN_MIND, LINTS
from narrowpy.mcp import run_check

CYCLE = {"pkg/__init__.py": "", "pkg/a.py": "from . import b\n", "pkg/b.py": "from . import a\n"}


def load(root: Path, toml: str) -> Config:
    (root / "pyproject.toml").write_text(toml, encoding="utf-8")
    return Config.load(root)


def test_no_pyproject_means_defaults(tmp_path: Path) -> None:
    cfg = Config.load(tmp_path)
    assert (cfg.lints, cfg.max_interface, cfg.include_tests, cfg.exclude) == (list(LINTS), HELD_IN_MIND, False, [])
    assert cfg.problems == []


def test_every_setting_is_read(tmp_path: Path) -> None:
    cfg = load(tmp_path, '[tool.narrowpy]\nexclude = ["a/*"]\ninclude-tests = true\nmax-interface = 3\n'
                         'lints = ["tangle"]\nbaseline = "b.json"\n')
    assert (cfg.exclude, cfg.include_tests, cfg.max_interface, cfg.lints, cfg.baseline) == (
        ["a/*"], True, 3, ["tangle"], "b.json")
    assert cfg.problems == []


def test_typo_in_a_lint_name_cannot_turn_every_check_off(tmp_path: Path) -> None:
    cfg = load(tmp_path, '[tool.narrowpy]\nlints = ["modul-cycle"]\n')
    assert cfg.lints == list(LINTS)  # fell back to all, not to none
    assert any("modul-cycle" in p for p in cfg.problems)


def test_one_good_name_keeps_the_good_ones_and_reports_the_bad(tmp_path: Path) -> None:
    cfg = load(tmp_path, '[tool.narrowpy]\nlints = ["tangle", "nope"]\n')
    assert cfg.lints == ["tangle"]
    assert any("nope" in p for p in cfg.problems)


def test_empty_lint_list_is_honoured_but_reported(tmp_path: Path) -> None:
    cfg = load(tmp_path, "[tool.narrowpy]\nlints = []\n")
    assert cfg.lints == []
    assert any("no check runs" in p for p in cfg.problems)


@pytest.mark.parametrize("toml, fragment", [
    ('[tool.narrowpy]\ninclude_tests = true\n', "unknown setting `include_tests`"),
    ('[tool.narrowpy]\nexclude = "src/*"\n', "`exclude` must be a list"),
    ('[tool.narrowpy]\ninclude-tests = "yes"\n', "`include-tests` must be true or false"),
    ('[tool.narrowpy]\nmax-interface = true\n', "`max-interface` must be a whole number"),
    ('[tool.narrowpy]\nmax-interface = "7"\n', "`max-interface` must be a whole number"),
    ('[tool.narrowpy]\nlints = "tangle"\n', "`lints` must be a list"),
    ('[tool.narrowpy]\nbaseline = 3\n', "`baseline` must be a file name"),
    ('[tool]\nnarrowpy = 3\n', "must be a table"),
    ('[tool.narrowpy\nx =', "could not be read"),
])
def test_bad_settings_are_reported_and_defaults_stay(tmp_path: Path, toml: str, fragment: str) -> None:
    cfg = load(tmp_path, toml)
    assert any(fragment in p for p in cfg.problems), cfg.problems
    assert cfg.lints == list(LINTS) and cfg.max_interface == HELD_IN_MIND and not cfg.include_tests


def test_max_interface_is_at_least_one(tmp_path: Path) -> None:
    assert load(tmp_path, "[tool.narrowpy]\nmax-interface = 0\n").max_interface == 1


def test_cli_prints_config_problems_and_still_checks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(tmp_path, CYCLE)
    (tmp_path / "pyproject.toml").write_text('[tool.narrowpy]\nlints = ["modul-cycle"]\n')
    assert main([str(tmp_path), "--no-baseline"]) == 1  # the cycle is still found
    err = capsys.readouterr().err
    assert "config:" in err and "modul-cycle" in err


def test_mcp_tool_shows_config_problems(tmp_path: Path) -> None:
    write_files(tmp_path, CYCLE)
    (tmp_path / "pyproject.toml").write_text('[tool.narrowpy]\nlints = ["modul-cycle"]\n')
    text, data, is_error = run_check({"path": str(tmp_path), "all": True})
    assert not is_error and "config:" in text and len(data["findings"]) == 1


def names(root: Path, cfg: Config | None = None) -> list[str]:
    return sorted(s.module for s in find_sources(root, cfg or Config()))


def test_discovery_skips_tooling_dirs_tests_and_hidden(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "app/__init__.py": "", "app/core.py": "", "app/test_core.py": "", "app/conftest.py": "",
        "app/core_test.py": "", "tests/test_x.py": "", "docs/conf.py": "", "examples/demo.py": "",
        ".venv/lib/x.py": "", "build/b.py": "", "node_modules/n.py": "", ".hidden/h.py": "",
        "app.egg-info/e.py": "", "notes.txt": "",
    })
    assert names(tmp_path) == ["app", "app.core"]


def test_include_tests_brings_tests_docs_and_examples_back(tmp_path: Path) -> None:
    write_files(tmp_path, {"app/__init__.py": "", "tests/t.py": "", "app/test_core.py": "", "examples/demo.py": ""})
    cfg = Config(include_tests=True)
    assert {"app", "app.test_core", "t", "demo"} <= set(names(tmp_path, cfg))


def test_exclude_globs_match_directories_and_files(tmp_path: Path) -> None:
    write_files(tmp_path, {"src/legacy/old.py": "", "src/new.py": "", "scripts/run.py": "", "src/gen_pb2.py": ""})
    cfg = Config(exclude=["src/legacy", "scripts/*", "*_pb2.py"])
    assert names(tmp_path, cfg) == ["new"]


def test_module_name_climbs_only_through_packages(tmp_path: Path) -> None:
    write_files(tmp_path, {"top/__init__.py": "", "top/sub/__init__.py": "", "top/sub/leaf.py": "",
                           "loose/script.py": ""})
    assert module_name(tmp_path / "top/sub/leaf.py", tmp_path) == ("top.sub.leaf", False)
    assert module_name(tmp_path / "top/sub/__init__.py", tmp_path) == ("top.sub", True)
    assert module_name(tmp_path / "loose/script.py", tmp_path) == ("script", False)


def test_second_file_claiming_a_module_name_is_skipped(tmp_path: Path) -> None:
    write_files(tmp_path, {"a/tool.py": "x = 1\n", "b/tool.py": "x = 2\n"})
    found = find_sources(tmp_path, Config())
    assert [s.rel for s in found] == ["a/tool.py"]


def test_command_line_flags_win_over_the_file(tmp_path: Path) -> None:
    cfg = load(tmp_path, '[tool.narrowpy]\nexclude = ["a/*"]\nmax-interface = 3\nlints = ["tangle"]\n')
    cfg.override(exclude=["b/*"], include_tests=True, max_interface=9, lints=["field-leak"])
    assert (cfg.exclude, cfg.include_tests, cfg.max_interface, cfg.lints) == (["a/*", "b/*"], True, 9, ["field-leak"])
    cfg.override()  # no flags: nothing changes
    assert (cfg.exclude, cfg.max_interface, cfg.lints) == (["a/*", "b/*"], 9, ["field-leak"])
