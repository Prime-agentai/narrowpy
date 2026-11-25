# narrowpy on real projects

Run on 2026-11-25 against shallow clones, with default settings (tests, docs and examples are
treated as outsiders and skipped). Timings are wall-clock on a 6-CPU Linux box, Python 3.12.

| Project | Commit | Modules | Findings | module-cycle | tangle | field-leak | shallow-module | Time |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| [aider](https://github.com/Aider-AI/aider) | 5dc9490 | 96 | 9 | 2 | 0 | 6 | 1 | 1.6 s |
| [httpx](https://github.com/encode/httpx) | 7cff5f7 | 23 | 9 | 1 | 0 | 8 | 0 | 0.5 s |
| [rich](https://github.com/Textualize/rich) | 3a109cc | 105 | 14 | 2 | 2 | 7 | 3 | 1.9 s |
| [browser-use](https://github.com/browser-use/browser-use) | 94784c1 | 49 | 21 | 3 | 0 | 16 | 2 | 1.4 s |

These are well-run projects. The point is not that they are sloppy. It is that every mature
codebase has *some* findings, which is why the gate is a ratchet (`narrowpy baseline`) and not a
wall.

## Hidden cycles are the common case

Every cycle note says whether the circle exists when Python imports the modules, or only through
imports moved into functions or under `TYPE_CHECKING`:

- **browser-use**: 16 modules in `browser_use` form one circle. **97 of its 142 imports are hidden**,
  and at import time there is no circle at all. Hidden imports are the only thing holding it
  together, so the cycle is invisible to Python and fully visible to anyone reading the code.
- **aider**: `aider.coders` ↔ `aider.commands`, with 3 of 4 imports function-local. Same pattern.
- **httpx**: 11 private modules in one circle, closed only by 7 TYPE_CHECKING or function-local imports.
- **rich**: `rich.cells` ↔ `rich._unicode_data` is a real import-time cycle. The large 44-module
  circle is closed only by hidden imports.

## Field leaks, checked by hand

12 field-leak findings were checked against the source, across all four projects. All 12 match
the rule as defined. Examples:

- aider `main.py:524`: `litellm._lazy_module.client_session = httpx.Client(verify=False)` writes
  through another class's private field.
- aider `coders/context_coder.py:17`: `self.repo_map.refresh = "always"`; three of `RepoMap`'s
  settings are rewritten from outside.
- browser-use `agent/service.py:464-473`: `Agent` reads and writes `Browser._user` to track
  ownership. That is two classes sharing one private field.
- httpx `_client.py:953`: `Client` assigns `response.history` and `response.next_request` on
  `Response` objects it did not build.
- rich `live.py:221`: `Live` reads `Console._live_stack`; `text.py:1125` reads `Lines._lines`.

Calibration from this pass is now built into the rules:

- Reading an attribute *of* a field (`request.url.host`) is a read, not a leak. This removed an
  httpx finding that counted 28 such places on `Request.url`.
- Calling a method on a field that holds a plain value (`request.method.encode()`, where
  `self.method = method.upper()` and `method: str`), or an immutable project object (a frozen
  dataclass, a NamedTuple, or a class that only sets fields while being built), is a read.
- `if __name__ == "__main__":` blocks are script code. rich's demo blocks had been pulling
  `rich.__main__` into the cycle.

## What it does not see

The typer is deliberately conservative. A receiver gets a type only from annotations, constructor
calls, annotated returns, iteration over annotated containers and `with ... as`. There is no
`isinstance` narrowing, and no inference across untyped calls. Unannotated code produces fewer
findings, not wrong ones, with one exception: a private field (`_name`) whose name belongs to
exactly one project class is matched by name, and the note says so.
