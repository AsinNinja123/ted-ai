"""
dashboard/app.py — Ted's memory dashboard (Flask).

Run:  python -m dashboard          (from ~/ted-ai, inside the venv)
Then: http://127.0.0.1:5175

Reads and writes data/memory.db directly — the same file Ted uses. Every
change made here (and every change Ted makes) lands in the memory_audit
table, shown in the History tab.
"""

import os
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file

from dashboard import db

app = Flask(__name__)
_HERE = os.path.dirname(os.path.abspath(__file__))


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


@app.get("/api/version")
def api_version():
    """Lets the HUD (and hud.py) verify the server on this port speaks the
    chat API — an older dashboard process holding the port 404s this."""
    return jsonify({"version": 2, "chats": True})


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
    return jsonify(db.list_chats())


@app.post("/api/chats")
def api_chat_create():
    return jsonify({"id": db.create_chat()})


@app.get("/api/chats/<int:chat_id>")
def api_chat_get(chat_id):
    try:
        return jsonify(db.get_chat(chat_id))
    except KeyError as e:
        return jsonify({"error": str(e)}), 404


@app.delete("/api/chats/<int:chat_id>")
def api_chat_delete(chat_id):
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
                       "Give this chat a title (3-6 words, no quotes, no period) and a "
                       "one-sentence summary. Reply as exactly two lines:\n"
                       "TITLE: ...\nSUMMARY: ...\n\n" + transcript}],
            max_tokens=90, temperature=0.3, timeout=8.0)
        for line in (r.choices[0].message.content or "").splitlines():
            if line.upper().startswith("TITLE:"):
                title = line[6:].strip().strip('"')
            elif line.upper().startswith("SUMMARY:"):
                summary = line[8:].strip()
    except Exception as e:
        print(f"[dashboard] chat summarize fell back: {e}")

    if not title:
        title = user_turns[0]["content"][:48] + ("…" if len(user_turns[0]["content"]) > 48 else "")
    db.set_chat_meta(chat_id, title=title, summary=summary)
    return jsonify({"title": title, "summary": summary or chat["summary"]})


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
