# Changelog

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
