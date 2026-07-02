"""Use-case tests for the sales tally. Uses a temp log file."""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import sales
sales.LOG_PATH = os.path.join(tempfile.mkdtemp(), "sales_test.json")

PASS = FAIL = 0
def check(desc, cond):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else:    FAIL += 1; print(f"  ✗ {desc}")

check("empty day message", "Nothing logged" in sales.today_summary())
r = sales.log_sale(3, "Excaliburs")
check("log confirms with running total", "3 Excaliburs" in r and "3 units" in r)
sales.log_sale(12, "roman candles")
sales.log_sale(1, "excalibur")           # singular/plural tallies together
s = sales.today_summary()
check("summary totals 16 units / 3 sales", "16 units" in s and "3 sales" in s)
check("plural+singular merged", "4 excalibur" in s)
check("undo removes the last entry", "excalibur" in sales.undo_last())
check("total drops to 15", "15 units" in sales.today_summary())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
