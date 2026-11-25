#!/usr/bin/env bash
# Types, tests, and the CLI end to end (lint, ratchet, Stop hook, MCP). Usage: PYTHON=python3 bash scripts/check.sh
set -euo pipefail
trap 'echo "check.sh: failed at line $LINENO" >&2' ERR
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
export PYTHONPATH="$PWD/src" PYTHONDONTWRITEBYTECODE=1 NO_COLOR=1
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.invalid GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.invalid

"$PY" -m mypy
"$PY" -m mypy --platform win32
"$PY" -m mypy --platform darwin
"$PY" -m pytest -q

work="$(mktemp -d)"
np() { "$PY" -m narrowpy "$@"; }
# A Stop/SessionStart event for the current directory, built by Python so the path is native
# (on Windows, Git Bash would hand over an MSYS path that Python cannot open).
ev() { "$PY" -c 'import json, os, sys; e = {"session_id": "e2e", "cwd": os.getcwd()}; e.update({"stop_hook_active": True} if sys.argv[1:] else {}); print(json.dumps(e))' "$@"; }

echo "== a sloppy app: one of each problem, exit 1"
"$PY" tests/sloppy.py "$work/app"
code=0; np check "$work/app" > "$work/out.txt" || code=$?
[ "$code" = 1 ] || { echo "expected exit 1, got $code" >&2; cat "$work/out.txt"; exit 1; }
grep -q "error\[module-cycle\]" "$work/out.txt"
grep -q "only the hidden imports close it" "$work/out.txt"
grep -q "RunQueue.items. is known outside .app.queue.: 2 places" "$work/out.txt"
grep -q "4 findings (all findings)" "$work/out.txt"

echo "== ratchet: baseline, then only new or worse findings fail"
cd "$work/app"
git init -q -b main && git add -A && git commit -qm init
np baseline > /dev/null
np check > "$work/out.txt"
grep -q "4 known findings not shown" "$work/out.txt"
git add -A && git commit -qm baseline

echo "== Stop hook: blocks a hidden-import regression, then lets go once told"
np hook install > /dev/null
grep -q "hook stop" .claude/settings.json
ev | np hook session-start
ev | np hook stop > "$work/stop0.txt"
[ ! -s "$work/stop0.txt" ] || { echo "clean tree must not block" >&2; cat "$work/stop0.txt"; exit 1; }
cat >> app/report.py <<'PYEOF'


def render() -> str:
    from app.policy import admit  # the agent's "fix" for a circular import
    return admit.__name__
PYEOF
cat >> app/policy.py <<'PYEOF'


def describe(q: RunQueue) -> str:
    from app.report import backlog
    return str(backlog(q))
PYEOF
ev | np hook stop > "$work/stop1.txt"
"$PY" - "$work/stop1.txt" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["decision"] == "block", d
assert "module-cycle" in d["reason"] and "[new]" in d["reason"], d["reason"]
assert "app.policy" in d["reason"] and "app.report" in d["reason"]
PYEOF
ev active | np hook stop > "$work/stop2.txt"
grep -q systemMessage "$work/stop2.txt"
code=0; np check --base HEAD --format github > "$work/gh.txt" || code=$?
[ "$code" = 1 ]
grep -q "^::error file=app/" "$work/gh.txt"

echo "== Stop hook: re-baselining the regression away is blocked too"
np baseline > /dev/null
np check > /dev/null
ev | np hook stop > "$work/stop3.txt"
grep -q "changed since the session started" "$work/stop3.txt"
git checkout -q -- .narrowpy-baseline.json app

echo "== MCP: initialize, list, call"
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"narrowpy_check","arguments":{"all":true}}}' \
  | np mcp > "$work/mcp.txt"
grep -q '"serverInfo"' "$work/mcp.txt"
grep -q 'field-leak:app.queue.RunQueue.items' "$work/mcp.txt"
cd - > /dev/null

echo "== narrowpy lints itself: zero findings"
np check src --no-baseline
echo "check.sh: all green"
