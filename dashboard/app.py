"""
dashboard/app.py — Ted's memory dashboard (Flask).

Run:  python -m dashboard          (from ~/ted-ai, inside the venv)
Then: http://127.0.0.1:5175

Reads and writes data/memory.db directly — the same file Ted uses. Every
change made here (and every change Ted makes) lands in the memory_audit
table, shown in the History tab.
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 15 (§15.1 – §15.2)
# =============================================================================
#
#  WHAT THIS FILE IS
#      A small Flask web server on 127.0.0.1:5175. Flask is a Python library for
#      writing web servers: you write a function, put a @app.route("/path") line
#      above it, and that function now answers requests to that URL.
#
#      This is what the Memory, Notebook and Diagnostics panels inside Ted's window
#      are actually showing — they are web pages in an iframe, served from here. It
#      also runs standalone: `python -m dashboard`.
#
#  IT WRITES THE SAME FILE TED DOES
#      data/memory.db. Two processes, one database. That is why the audit log is
#      implemented as SQLite triggers rather than as dashboard code — see
#      dashboard/db.py.
#
# =============================================================================

import os
import sys
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file

from dashboard import db

app = Flask(__name__)
_HERE = os.path.dirname(os.path.abspath(__file__))


def _neutral_chat_label(value, fallback=""):
    """Strip model narration from sidebar titles and chat summaries."""
    import re
    text = (value or "").strip().strip('"').strip()
    text = re.sub(
        r"^(?:ted(?:'s)?\s+(?:helps?|assists?|handles?|discusses?|works?|"
        r"responds?|shares?|reminds?|updates?|manages?|directs?|provides?)|"
        r"(?:the\s+)?user\s+(?:asks?|requests?|wants?|needs?))"
        r"(?:\s+(?:with|about|on|to))?\s+",
        "", text, flags=re.I)
    text = re.sub(r"^(?:help(?:ing)?\s+with)\s+", "", text, flags=re.I)
    text = re.sub(r"^ted(?:'s)?\s+", "", text, flags=re.I)
    text = re.sub(r"^(?:and|with|about)\s+", "", text, flags=re.I)
    text = text.strip(" .:-")
    return text or fallback


def _neutral_chat_row(row):
    """Return chat metadata suitable for the sidebar, including old rows."""
    row = dict(row)
    raw_title = row.get("title", "")
    title = _neutral_chat_label(raw_title, "Conversation")
    raw_summary = row.get("summary", "")
    if __import__("re").match(r"^(?:ted(?:'s)?|(?:the\s+)?user|a\s+user)\b",
                              raw_summary or "", __import__("re").I):
        summary = title
    else:
        summary = _neutral_chat_label(raw_summary, title)
    row.update(title=title, summary=summary)
    return row


def _allowed_hud_origin(origin):
    """Accept Ted's local pywebview origin without opening memory to websites.

    pywebview serves local HTML through a random ``127.0.0.1`` HTTP port, so
    the HUD's browser origin is not reliably ``file://`` or ``null``. Only an
    exact loopback hostname over HTTP is trusted; public and LAN origins remain
    unable to read or mutate the dashboard API from browser JavaScript.
    """
    if origin in ("null", "file://"):
        return True
    try:
        parsed = urlsplit(origin or "")
        return (parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.port is not None
                and not parsed.username
                and not parsed.password
                and parsed.path in ("", "/")
                and not parsed.query
                and not parsed.fragment)
    except (TypeError, ValueError):
        return False


@app.after_request
def _cors(resp):
    """Permit Ted's local HUD while blocking arbitrary browser origins."""
    origin = request.headers.get("Origin")
    if _allowed_hud_origin(origin):
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Vary"] = "Origin"
    return resp


@app.get("/")
def index():
    return send_file(os.path.join(_HERE, "index.html"))


@app.get("/map")
def ted_map():
    """A plain-language page describing Ted, regenerated on every load.

    tools/ted_map.py reads the code, the database and git and renders the page
    from what it finds, so this cannot drift from reality the way a written
    document does. It is regenerated per request rather than cached because the
    facts it reports — branch, row counts, whether the daemon is alive — are
    exactly the ones that change while you are looking at them.
    """
    import importlib.util
    from flask import Response
    path = os.path.join(os.path.dirname(_HERE), "tools", "ted_map.py")
    if not os.path.exists(path):
        return Response("tools/ted_map.py is missing.", mimetype="text/plain",
                        status=404)
    try:
        spec = importlib.util.spec_from_file_location("ted_map", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return Response(mod.render(mod.collect()), mimetype="text/html")
    except Exception as e:
        # A broken map page must never take the memory dashboard down with it.
        return Response(f"Could not build the map: {e}", mimetype="text/plain",
                        status=500)


@app.get("/api/version")
def api_version():
    """Lets the HUD (and hud.py) verify the server on this port speaks the
    chat API — an older dashboard process holding the port 404s this."""
    return jsonify({"version": 5, "chats": True, "map": True,
                    "diagnostics": True, "notebook": True})


_weather_cache = {"ts": 0.0, "data": None}


@app.get("/api/weather")
def api_weather():
    """Compact current weather for the HUD clock widget. Open-Meteo (no key),
    location from core.assistant when available. Cached 10 minutes."""
    import time as _time
    if _weather_cache["data"] and _time.time() - _weather_cache["ts"] < 600:
        return jsonify(_weather_cache["data"])
    try:
        import json as _json
        import urllib.request
        from core.assistant import get_location
        loc = get_location() or {}
        lat, lon = loc.get("lat"), loc.get("lon")
        if lat is None:
            return jsonify({"error": "no location"}), 503
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               f"&current_weather=true&daily=temperature_2m_max,temperature_2m_min"
               f"&temperature_unit=fahrenheit&forecast_days=1&timezone=auto")
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = _json.loads(r.read())
        codes = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
                 45: "Fog", 48: "Fog", 51: "Drizzle", 53: "Drizzle", 55: "Drizzle",
                 61: "Rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain",
                 67: "Freezing rain", 71: "Snow", 73: "Snow", 75: "Heavy snow",
                 77: "Snow", 80: "Showers", 81: "Showers", 82: "Heavy showers",
                 85: "Snow showers", 86: "Snow showers", 95: "Thunderstorm",
                 96: "Thunderstorm", 99: "Thunderstorm"}
        cw = data.get("current_weather", {})
        daily = data.get("daily", {})
        out = {"temp": round(cw.get("temperature", 0)),
               "desc": codes.get(cw.get("weathercode"), ""),
               "hi": round((daily.get("temperature_2m_max") or [0])[0]),
               "lo": round((daily.get("temperature_2m_min") or [0])[0]),
               "city": loc.get("city", "")}
        _weather_cache.update(ts=_time.time(), data=out)
        return jsonify(out)
    except Exception as e:
        return jsonify({"error": str(e)}), 503


@app.get("/api/summary")
def api_summary():
    return jsonify(db.summary())


def _check_table(table):
    if table not in db.TABLES:
        return jsonify({"error": f"unknown table '{table}'"}), 404
    return None


@app.get("/api/rows/<table>")
def api_rows(table):
    err = _check_table(table)
    if err:
        return err
    return jsonify(db.list_rows(
        table,
        search=request.args.get("q", ""),
        limit=min(int(request.args.get("limit", 100)), 500),
        offset=int(request.args.get("offset", 0)),
    ))


@app.post("/api/rows/<table>")
def api_create(table):
    err = _check_table(table)
    if err:
        return err
    try:
        pk = db.create_row(table, request.get_json(force=True) or {})
        return jsonify({"ok": True, "pk": pk})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.put("/api/rows/<table>/<int:pk>")
def api_update(table, pk):
    err = _check_table(table)
    if err:
        return err
    try:
        db.update_row(table, pk, request.get_json(force=True) or {})
        return jsonify({"ok": True})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


@app.delete("/api/rows/<table>/<int:pk>")
def api_delete(table, pk):
    err = _check_table(table)
    if err:
        return err
    try:
        db.delete_row(table, pk)
        return jsonify({"ok": True})
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


@app.get("/api/history")
def api_history():
    return jsonify(db.list_history(
        table=request.args.get("table", ""),
        actor=request.args.get("actor", ""),
        action=request.args.get("action", ""),
        search=request.args.get("q", ""),
        limit=min(int(request.args.get("limit", 100)), 500),
        offset=int(request.args.get("offset", 0)),
    ))


# ---------------------------------------------------------------------------
# Chat sessions — backing for the HUD's Claude-style sidebar.
# ---------------------------------------------------------------------------

@app.get("/api/chats")
def api_chats():
    # ?include_hidden=1 is the memory dashboard's view: everything, including
    # threads deleted from the sidebar. The HUD never passes it.
    include_hidden = request.args.get("include_hidden") in ("1", "true", "yes")
    return jsonify([_neutral_chat_row(chat)
                    for chat in db.list_chats(include_hidden=include_hidden)])


@app.get("/api/chats/search")
def api_chat_search():
    """Full-text search over what was said, for the sidebar search box.

    Registered before /api/chats/<int:chat_id> matters not at all to Flask —
    the int converter cannot match "search" — but the reader lives in
    core/memory.py either way, so Ted's tool and this box return the same rows.
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    from core import memory
    try:
        limit = min(int(request.args.get("limit", 30)), 100)
    except ValueError:
        limit = 30
    return jsonify(memory.search_chat_turns(q, limit=limit))


@app.post("/api/chats")
def api_chat_create():
    return jsonify({"id": db.create_chat()})


@app.get("/api/chats/<int:chat_id>")
def api_chat_get(chat_id):
    try:
        return jsonify(_neutral_chat_row(db.get_chat(chat_id)))
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


@app.post("/api/chats/<int:chat_id>/hidden")
def api_chat_hidden(chat_id):
    """Soft delete and its undo — what the sidebar's × calls.

    Deliberately not wired to DELETE. DELETE on this resource destroys turns,
    and an endpoint that sometimes destroys and sometimes hides is one typo
    away from destroying when it meant to hide.
    """
    body = request.get_json(silent=True) or {}
    hidden = body.get("hidden", True)
    try:
        return jsonify({"ok": True, "hidden": db.set_chat_hidden(chat_id, hidden)})
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


@app.delete("/api/chats/<int:chat_id>")
def api_chat_delete(chat_id):
    """Hard delete: thread and turns, unrecoverable. Memory dashboard only."""
    try:
        db.delete_chat(chat_id)
        return jsonify({"ok": True})
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


@app.post("/api/chats/<int:chat_id>/turns")
def api_chat_turn(chat_id):
    body = request.get_json(force=True) or {}
    try:
        tid = db.add_chat_turn(chat_id, body.get("role", ""), body.get("content", ""))
        return jsonify({"ok": True, "id": tid})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/chats/<int:chat_id>/summarize")
def api_chat_summarize(chat_id):
    """Auto-title/summarize a chat, Claude-style. Groq when available;
    falls back to the first user message truncated."""
    try:
        chat = db.get_chat(chat_id)
    except KeyError as e:
        return jsonify({"error": str(e)}), 404
    user_turns = [t for t in chat["turns"] if t["role"] == "user"]
    if not user_turns:
        return jsonify({"title": chat["title"], "summary": chat["summary"]})

    title, summary = None, None
    try:
        from core.llm import chat_create
        transcript = "\n".join(
            f"{'User' if t['role'] == 'user' else 'Ted'}: {t['content'][:400]}"
            for t in chat["turns"][-20:])
        r = chat_create(
            messages=[{"role": "user", "content":
                       "Label this chat neutrally. The title must be a 3-6 word noun "
                       "phrase, never a narrated sentence. The summary must also name "
                       "the topic directly. Never write 'Ted helps', 'Ted discusses', "
                       "'the user asks', or describe either person acting. Good examples: "
                       "'Digital Task Requests', 'Browser and Screen Control', "
                       "'Spotify Playback Bug'. Reply as exactly two lines:\n"
                       "TITLE: ...\nSUMMARY: ...\n\n" + transcript}],
            max_tokens=90, temperature=0.3, timeout=8.0,
            _ted_workload="background")
        for line in (r.choices[0].message.content or "").splitlines():
            if line.upper().startswith("TITLE:"):
                title = line[6:].strip().strip('"')
            elif line.upper().startswith("SUMMARY:"):
                summary = line[8:].strip()
    except Exception as e:
        print(f"[dashboard] chat summarize fell back: {e}")

    if not title:
        title = user_turns[0]["content"][:48] + ("…" if len(user_turns[0]["content"]) > 48 else "")
    title = _neutral_chat_label(title, "Conversation")
    summary = _neutral_chat_label(summary, title)
    db.set_chat_meta(chat_id, title=title, summary=summary)
    return jsonify({"title": title, "summary": summary or chat["summary"]})


# `python -m dashboard` runs with the repo root on sys.path already, but the
# same module is imported from hud.py's thread and, in tests, directly. Make
# `core` importable either way rather than assuming a working directory.
_PROJECT_ROOT = os.path.dirname(_HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ─── diagnostics: what Ted actually did, per turn ──────────────────────────
#
# This exists because Charlie was reading Ted's behaviour out of a terminal.
# Everything here is recorded by core/telemetry.py on the reply path and only
# read back here, so a broken dashboard cannot affect a conversation.

@app.get("/diagnostics")
def diagnostics_page():
    return send_file(os.path.join(_HERE, "diagnostics.html"))


@app.get("/api/diagnostics/turns")
def api_diag_turns():
    from core import telemetry
    limit = min(int(request.args.get("limit", 60)), 500)
    offset = max(int(request.args.get("offset", 0)), 0)
    return jsonify(telemetry.recent(limit, offset))


@app.get("/api/diagnostics/stats")
def api_diag_stats():
    from core import telemetry
    window = float(request.args.get("window", 3600))
    return jsonify(telemetry.stats(window))


@app.get("/api/diagnostics/report")
def api_diag_report():
    """Plain text, for pasting somewhere else. The way this project is
    actually debugged is Charlie handing a session to another model."""
    from core import telemetry
    from flask import Response
    limit = min(int(request.args.get("limit", 25)), 200)
    return Response(telemetry.as_report(limit), mimetype="text/plain")


@app.post("/api/diagnostics/clear")
def api_diag_clear():
    from core import telemetry
    return jsonify({"cleared": telemetry.clear()})


@app.get("/api/provider")
def api_provider_get():
    """Which brain is pinned, which one answered last, and whether the local
    one would actually work if selected."""
    from core import providers
    return jsonify({
        "mode": providers.get_provider_mode(),
        "active": providers.active_provider(),
        "active_model": providers.active_model(),
        "cloud_model": providers.CLOUD_CHAT_MODEL,
        "local_model": providers.LOCAL_CHAT_MODEL,
        "local_tool_model": providers.LOCAL_TOOL_MODEL,
        "cloud_configured": providers.groq_client() is not None,
        "local_ready": providers.local_model_ready(),
        # Pulled is not loaded. The HUD needs the difference to decide whether
        # a slow turn deserves "loading the local model" or just patience.
        "local_warm": providers.local_model_warm(),
        "last_cloud_error": providers.last_cloud_error(),
    })


@app.post("/api/provider")
def api_provider_set():
    from core import providers
    mode = (request.get_json(silent=True) or {}).get("mode", "")
    try:
        providers.set_provider_mode(mode)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"mode": providers.get_provider_mode()})


# ---------------------------------------------------------------------------
# User-defined phrase -> action routines.
# ---------------------------------------------------------------------------

@app.get("/api/routines")
def api_routines_list():
    from core import routines
    return jsonify(routines.list_routines())


@app.get("/api/routines/actions")
def api_routine_actions():
    from core import routines
    return jsonify(routines.ROUTINE_ACTIONS)


@app.get("/api/routines/match")
def api_routine_match():
    """Preview matching only. The dashboard never executes Mac actions."""
    from core import routines
    match = routines.match_routine(request.args.get("q", ""))
    return jsonify({"match": match})


@app.post("/api/routines")
def api_routine_create():
    from core import routines
    try:
        return jsonify(routines.save_routine(request.get_json(force=True) or {}))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.put("/api/routines/<int:routine_id>")
def api_routine_update(routine_id):
    from core import routines
    try:
        return jsonify(routines.save_routine(
            request.get_json(force=True) or {}, routine_id=routine_id))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


@app.delete("/api/routines/<int:routine_id>")
def api_routine_delete(routine_id):
    from core import routines
    try:
        routines.delete_routine(routine_id)
        return jsonify({"ok": True})
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


# Charlie's shorthand is interpretation state, not a personal fact. Keeping it
# beside Routines makes the distinction visible and lets edits affect a running
# Ted immediately without retraining or restarting.
@app.get("/api/lingo")
def api_lingo_list():
    from core import lingo
    return jsonify(lingo.list_terms())


@app.get("/api/lingo/expand")
def api_lingo_expand():
    from core import lingo
    expanded, matched = lingo.expand(request.args.get("q", ""))
    return jsonify({"expanded": expanded, "matched": matched})


@app.post("/api/lingo")
def api_lingo_create():
    from core import lingo
    try:
        return jsonify(lingo.save_term(request.get_json(force=True) or {}))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.put("/api/lingo/<int:term_id>")
def api_lingo_update(term_id):
    from core import lingo
    try:
        return jsonify(lingo.save_term(
            request.get_json(force=True) or {}, term_id=term_id))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


@app.delete("/api/lingo/<int:term_id>")
def api_lingo_delete(term_id):
    from core import lingo
    try:
        lingo.delete_term(term_id)
        return jsonify({"ok": True})
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


# ---------------------------------------------------------------------------
# The notebook — Ted's own pages, and the lined-paper view of them.
# The same core/notebook.py the tools use, so what Ted writes and what shows up
# here can never be two different things.
# ---------------------------------------------------------------------------

@app.get("/notebook")
def notebook_page():
    return send_file(os.path.join(_HERE, "notebook.html"))


@app.get("/api/notebook")
def api_notebook_pages():
    from core import notebook
    return jsonify(notebook.list_pages())


@app.get("/api/notebook/<path:name>")
def api_notebook_read(name):
    from core import notebook
    try:
        doc = notebook.read_page(name, limit=0)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if doc is None:
        return jsonify({"error": "no such page"}), 404
    return jsonify(doc)


@app.post("/api/notebook")
def api_notebook_create():
    from core import notebook
    data = request.get_json(force=True) or {}
    try:
        if data.get("text"):
            page, number, made = notebook.add_entry(
                data.get("page", ""), data["text"], writer="user")
            return jsonify({"page": page, "entry": number, "created": made})
        return jsonify(notebook.create_page(data.get("page", "")))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.put("/api/notebook/<path:name>/<int:number>")
def api_notebook_edit(name, number):
    from core import notebook
    data = request.get_json(force=True) or {}
    try:
        page, resolved = notebook.edit_entry(
            name, number, data.get("text", ""), writer="user")
        return jsonify({"page": page, "entry": resolved})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


@app.delete("/api/notebook/<path:name>/<int:number>")
def api_notebook_delete_entry(name, number):
    from core import notebook
    try:
        page, resolved, body = notebook.delete_entry(name, number)
        return jsonify({"page": page, "entry": resolved, "removed": body})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


@app.put("/api/notebook/<path:name>")
def api_notebook_rename(name):
    from core import notebook
    data = request.get_json(force=True) or {}
    try:
        old, new = notebook.rename_page(name, data.get("name", ""))
        return jsonify({"was": old, "now": new})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


@app.delete("/api/notebook/<path:name>")
def api_notebook_delete_page(name):
    from core import notebook
    try:
        page, count = notebook.delete_page(name)
        return jsonify({"page": page, "removed": count})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


@app.get("/api/meta")
def api_meta():
    """Table registry for the frontend: columns, editable fields, labels."""
    return jsonify({
        t: {"cols": c["cols"], "editable": c["editable"], "required": c["required"]}
        for t, c in db.TABLES.items()
    })


def main(port=5175):
    db.get_conn()          # fail fast + ensure schema before serving
    print(f"[dashboard] Ted memory dashboard → http://127.0.0.1:{port}")
    print(f"[dashboard] database: {db.DB_PATH}")
    app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()
