# Live run: Claude Code hides a circular import, the Stop hook catches it

Run on 2026-11-25 with Claude Code 2.1.330 and `--model haiku`, on Linux with Python 3.12, in a
scratch git repo. Nothing below is staged: the transcript is quoted, and the diff is what the
agent left behind.

## Setup

A 3-file app. `shop/emails.py` imports `Order` from `shop/orders.py` to annotate `receipt()`.
`narrowpy hook install` added the SessionStart and Stop hooks to `.claude/settings.json`, and
both were committed. `narrowpy check` reported 0 findings.

The task forces the classic trap. `Order` has to call into `emails`, and `emails` already imports
`orders`:

```console
$ claude -p "Add a checkout() method to Order in shop/orders.py that emails the customer their
  receipt, using receipt() and send() from shop/emails.py (send it to the customer name). Then
  make sure 'python3 test_shop.py' still prints ok, and add a line to test_shop.py that calls
  checkout() on the test order." --model haiku --permission-mode acceptEdits \
  --allowedTools "Bash(python3:*)" "Bash(narrowpy:*)" Read Edit Write Glob Grep
```

## What happened (from the session transcript)

1. The agent read the files and said exactly what agents usually do:

   > `emails.py` imports `Order` from `orders.py`, so a top-level import in `orders.py` would be
   > circular. I'll import inside the method.

   It wrote `checkout()` with `from shop.emails import receipt, send` inside the method, plus a
   comment explaining the circular import. The tests passed, and it reported done.

2. **The Stop hook blocked.** This is the text the model received:

   ```
   narrowpy: your changes since 33e88c95c126 made the structure worse (1 new, 0 worse). Fix these before you stop:

   error[module-cycle] [new]: 2 modules in `shop` import each other in a circle: `shop.emails`, `shop.orders`
     --> shop/emails.py:3
     = cheapest tie to cut: `shop.emails` → `shop.orders` (1 import: shop/emails.py:3)
     = at import time there is no circle: only the hidden imports close it
     = 1 of 2 imports in this circle are hidden (function-local or TYPE_CHECKING): shop/orders.py:28
       shop.emails  ──imports──▶  shop.orders   (1×, first shop/emails.py:3)
       shop.orders  ──imports──▶  shop.emails   (1×, first shop/orders.py:28)  [function-local]

   how to fix (from first principles, not by moving code until the lint goes quiet):
     module-cycle: Decide which module is the lower layer and make every use point down. Moving an
     import into a function or under TYPE_CHECKING hides a cycle; it does not break it.
   ```

3. The agent fixed the design, not the symptom:

   > The cycle exists because `emails.py` imports `Order` only to annotate `receipt()`. I'll make
   > `emails.py` the lower layer: it defines a small `Protocol` describing what a receipt needs,
   > so it no longer imports `orders`. Then `orders.py` can import `emails` at the top.

4. The second Stop check found nothing new and ran in **244 ms**, so the agent stopped. The whole
   run took 21 s, 13 turns and $0.0064 (as reported by `claude -p --output-format json`).

## The diff the agent left

```diff
--- a/shop/emails.py
+++ b/shop/emails.py
-from shop.orders import Order
+from typing import Protocol


-def receipt(order: Order) -> str:
+class ReceiptSource(Protocol):
+    customer: str
+
+    def lines(self) -> list[str]: ...
+
+    def total(self) -> int: ...
+
+
+def receipt(order: ReceiptSource) -> str:
--- a/shop/orders.py
+++ b/shop/orders.py
+from shop.emails import receipt, send
 ...
+    def checkout(self) -> None:
+        send(self.customer, receipt(self))
```

```console
$ narrowpy check --base HEAD
narrowpy: 0 findings (new or worse since HEAD) in 3 modules, 3 classes, 9 functions [0.00s]
$ python3 test_shop.py
...
ok
```

`emails` no longer knows about `orders`. The dependency points one way, the import is at the top
where a reader expects it, and `receipt()` now states what it needs instead of naming a concrete
class.

## Not covered by this run

- One model (haiku) and one scenario. The `field-leak` and `shallow-module` paths through the
  hook are covered by `scripts/check.sh` and the tests, not by a live model run.
- The "blocked once, then allowed to stop" path (`stop_hook_active`) was not reached live: the
  agent fixed the cycle on the first block. `scripts/check.sh` exercises it.
