"""Focused regressions for local-network and provider lifecycle safety.

Run with the venv python:  python tests/test_safety.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import llm, remote, screen, tools  # noqa: E402
from dashboard.app import app, _allowed_hud_origin  # noqa: E402

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
pywebview_hud = client.get(
    "/api/version", headers={"Origin": "http://127.0.0.1:49152"})
check("dashboard does not grant arbitrary websites CORS access",
      "Access-Control-Allow-Origin" not in evil.headers)
check("file-based Ted HUD keeps dashboard CORS access",
      hud.headers.get("Access-Control-Allow-Origin") == "null")
check("WKWebView file origin can save chat turns",
      webkit_hud.headers.get("Access-Control-Allow-Origin") == "file://")
check("pywebview's random loopback origin can save chat turns",
      pywebview_hud.headers.get("Access-Control-Allow-Origin")
      == "http://127.0.0.1:49152")
check("lookalike and credential-bearing origins stay blocked",
      not _allowed_hud_origin("http://127.0.0.1.evil.test:49152")
      and not _allowed_hud_origin("http://user@127.0.0.1:49152")
      and not _allowed_hud_origin("https://127.0.0.1:49152"))

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

print("\n— Ted does not quit himself —")
from core import actions
check("the self-protection list is not empty",
      bool(actions._SELF_PROCESSES) and "" not in actions._SELF_PROCESSES)
for name in ("python", "python3", "ted", "terminal"):
    check(f"{name!r} is protected from 'close this app'",
          name in actions._SELF_PROCESSES)
check("MacAgent guards the same ground independently",
      {"Ted", "Terminal"} <= set(
          __import__("core.agents.mac", fromlist=["MacAgent"]).MacAgent.DEFAULT_PROTECTED_APPS))

print("\n" + "=" * 50)
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
