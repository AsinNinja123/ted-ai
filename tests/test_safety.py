"""Focused regressions for local-network and provider lifecycle safety.

Run with the venv python:  python tests/test_safety.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import llm, remote, screen, tools  # noqa: E402
from dashboard.app import app  # noqa: E402

PASS = FAIL = 0


def check(desc, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {desc}")
    else:
        FAIL += 1
        print(f"  ✗ {desc}")


print("— local service exposure —")
check("remote endpoint stays disabled without authentication",
      not remote._enabled(""))
check("a configured token enables the remote endpoint",
      remote._enabled("secret"))
check("authenticated remote endpoint can listen on the LAN",
      remote._bind_host("secret") == "0.0.0.0")

client = app.test_client()
evil = client.get("/api/version", headers={"Origin": "https://example.com"})
hud = client.get("/api/version", headers={"Origin": "null"})
webkit_hud = client.get("/api/version", headers={"Origin": "file://"})
check("dashboard does not grant arbitrary websites CORS access",
      "Access-Control-Allow-Origin" not in evil.headers)
check("file-based Ted HUD keeps dashboard CORS access",
      hud.headers.get("Access-Control-Allow-Origin") == "null")
check("WKWebView file origin can save chat turns",
      webkit_hud.headers.get("Access-Control-Allow-Origin") == "file://")

print("\n— provider lifecycle —")
retired = {"llama-3.1-8b-instant", "llama-3.3-70b-versatile",
           "meta-llama/llama-4-scout-17b-16e-instruct"}
check("chat fallback no longer targets a retiring model",
      llm.CHAT_FALLBACK_MODEL not in retired)
check("screen vision no longer targets the retired Scout model",
      screen.VISION_MODEL not in retired)
check("the availability fallback is genuinely local",
      "/" not in llm.CHAT_FALLBACK_MODEL and ":" in llm.CHAT_FALLBACK_MODEL)

print("\n— tool contracts —")
names = {(t.get("function") or {}).get("name") for t in tools.TOOL_SCHEMAS}
check("live web search is selected through the normal tool menu", "web_search" in names)
check("every tool rejects invented top-level arguments",
      all((t["function"].get("parameters") or {}).get("additionalProperties") is False
          for t in tools.TOOL_SCHEMAS))

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
