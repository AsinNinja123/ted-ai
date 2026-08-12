#!/usr/bin/env python3
"""tools/gate5_report.py — what gate 5 actually catches.

_assistant_command has ~300 branches and runs before the model gets a say.
Most of them shadow a tool that already exists, so most of them should be
deleted — but deleting from memory is guesswork. This reads the usage log
that core/app.py writes and ranks what really fired.

    python tools/gate5_report.py              # after a week of normal use
    python tools/gate5_report.py --days 3
    python tools/gate5_report.py --lines      # needs TED_GATE5_TRACE=1 runs

Reading it: anything absent from this report never fired and is dead weight.
Anything present still needs a human look — "a tool covers this" is the
common case even for branches that DO fire.
"""
import argparse
import collections
import datetime
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.paths import GATE5_LOG  # noqa: E402


def _normalize(t):
    """Collapse the variable parts so 'set a timer for 5 minutes' and
    '...for 20 minutes' land in the same bucket."""
    t = (t or "").lower().strip()
    t = re.sub(r"\d+", "#", t)
    return re.sub(r"\s+", " ", t)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0, help="only the last N days")
    ap.add_argument("--lines", action="store_true",
                    help="group by source line (needs TED_GATE5_TRACE=1)")
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()

    if not os.path.exists(GATE5_LOG):
        print(f"No log yet at {GATE5_LOG}.\nUse Ted normally for a few days, then re-run.")
        return

    cutoff = None
    if args.days:
        cutoff = datetime.datetime.now() - datetime.timedelta(days=args.days)

    rows = []
    with open(GATE5_LOG, encoding="utf-8") as fh:
        for ln in fh:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if cutoff:
                try:
                    if datetime.datetime.fromisoformat(r["t"]) < cutoff:
                        continue
                except Exception:
                    pass
            rows.append(r)

    if not rows:
        print("Log exists but has no rows in range.")
        return

    span = f"{rows[0]['t'][:10]} → {rows[-1]['t'][:10]}"
    print(f"\ngate 5 fired {len(rows)} times   ({span})\n")

    if args.lines:
        have = [r for r in rows if "line" in r]
        if not have:
            print("No line data — re-run Ted with TED_GATE5_TRACE=1 to collect it.")
            return
        counts = collections.Counter(r["line"] for r in have)
        print(f"{'hits':>6}  {'app.py line':>12}  example")
        for line, n in counts.most_common(args.top):
            ex = next(r["text"] for r in have if r["line"] == line)
            print(f"{n:>6}  {line:>12}  {ex[:60]}")
        print(f"\n{len(counts)} distinct branches fired.")
        return

    counts = collections.Counter(_normalize(r["text"]) for r in rows)
    print(f"{'hits':>6}  phrasing (digits collapsed to #)")
    for phrase, n in counts.most_common(args.top):
        print(f"{n:>6}  {phrase[:70]}")
    print(f"\n{len(counts)} distinct phrasings.")
    print("Re-run with --lines (and TED_GATE5_TRACE=1) to map these to branches.")


if __name__ == "__main__":
    main()
