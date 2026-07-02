"""core/sales.py — lightweight sales tally for the store.

Say "I sold 3 Excaliburs" and Ted logs it to data/sales_log.json.
Ask "how are sales today" / "close out the day" for a spoken summary.
No pricing, no inventory sync — just a fast running tally for busy days.
"""

import json
import os
import threading
from datetime import date, datetime

from core.paths import DATA

LOG_PATH = os.path.join(DATA, "sales_log.json")
_lock = threading.Lock()


def _load():
    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(entries):
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=1)


def _canon(item):
    """Normalize an item name so 'Excalibur' / 'excaliburs' tally together."""
    n = item.strip().lower().rstrip(".!,")
    if len(n) > 3 and n.endswith("s") and not n.endswith("ss"):
        n = n[:-1]
    return n


def log_sale(qty, item):
    """Append one sale. Returns the spoken confirmation."""
    with _lock:
        entries = _load()
        entries.append({"ts": datetime.now().isoformat(), "qty": int(qty),
                        "item": _canon(item)})
        _save(entries)
    total = sum(e["qty"] for e in entries
                if e["ts"][:10] == date.today().isoformat())
    unit = "unit" if qty == 1 else "units"
    return f"Logged — {qty} {item.strip()}. {total} {unit if total == 1 else 'units'} sold today."


def today_entries():
    today = date.today().isoformat()
    return [e for e in _load() if e["ts"][:10] == today]


def today_summary():
    """Spoken summary of today's tally, or a friendly empty message."""
    entries = today_entries()
    if not entries:
        return "Nothing logged today. Say 'I sold three Excaliburs' and I'll keep the tally."
    total = sum(e["qty"] for e in entries)
    per = {}
    for e in entries:
        per[e["item"]] = per.get(e["item"], 0) + e["qty"]
    top = sorted(per.items(), key=lambda kv: -kv[1])[:4]
    top_str = ", ".join(f"{n} {item}" for item, n in top)
    more = f", plus {len(per) - 4} more" if len(per) > 4 else ""
    return (f"{total} units across {len(entries)} sales today. "
            f"Top sellers: {top_str}{more}.")


def undo_last():
    """Remove the most recent sale entry. Returns spoken confirmation."""
    with _lock:
        entries = _load()
        if not entries:
            return "Nothing to undo."
        gone = entries.pop()
        _save(entries)
    return f"Scratched the last one — {gone['qty']} {gone['item']}."
