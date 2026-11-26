# Changelog

## Unreleased

- Fixed: a mistake in `[tool.narrowpy]` could quietly turn the gate off. A misspelled lint name
  (`lints = ["modul-cycle"]`) ran no checks and exited 0; a malformed `pyproject.toml` dropped every
  setting without a word. Config problems (unknown keys, wrong types, unknown lint names, bad TOML)
  are now printed (`check`, the Stop hook, the MCP tool) and the defaults stay on for whatever could
  not be read, so a mistake makes narrowpy check more, never less.
- Tests for config parsing, source discovery and every output format (31 -> 62 tests).

## 0.1.0 (2026-11-25)

First release.

- Four lints: `module-cycle` (sibling-level; function-local and TYPE_CHECKING imports count and are
  called out as hidden; each cycle says whether it exists at import time), `field-leak`, `tangle`
  and `shallow-module`.
- An annotation-driven receiver typer: stdlib only, conservative, no execution.
- Ratchet: `narrowpy baseline`, `check --base REV` (via `git archive`, cached per commit).
- Claude Code: SessionStart + Stop hooks, tamper check on the baseline and `[tool.narrowpy]`, block
  once and then notify.
- Output as text, json, sarif and GitHub annotations. MCP server (`narrowpy mcp`).
- Measured on aider, httpx, rich and browser-use (docs/real-world.md), plus a live Claude Code run
  (docs/live-claude-code.md).
