"""
core/remote.py — Simple HTTP endpoint for remote Ted control.

Exposes Ted over the local network so iOS Shortcuts or curl can talk to Ted.

Endpoints:
    POST /ask   {"text": "..."}   →  {"reply": "..."}
    GET  /ask?text=...            →  {"reply": "..."}   (easiest from Shortcuts)
    GET  /status                  →  {"status": "ok", "muted": bool}

Security: set REMOTE_TOKEN in config.py and every request must carry it —
either an X-Ted-Token header or a &token= query parameter. Without a token the
server stays disabled; it is never exposed unauthenticated.
"""

import threading
import logging
import hmac

try:
    from config import REMOTE_PORT
except Exception:
    REMOTE_PORT = 5150
try:
    from config import REMOTE_TOKEN
except Exception:
    REMOTE_TOKEN = ""


def _bind_host(token=REMOTE_TOKEN):
    """Expose the endpoint to the LAN only when authentication is configured."""
    return "0.0.0.0" if str(token).strip() else "127.0.0.1"


def _enabled(token=REMOTE_TOKEN):
    return bool(str(token).strip())


class RemoteServer:
    def __init__(self, api):
        self._api = api

    def start(self):
        if not _enabled():
            print("[remote] disabled — set REMOTE_TOKEN to enable authenticated LAN access")
            return
        try:
            from flask import Flask, request, jsonify
        except ImportError:
            print("[remote] flask not installed — HTTP endpoint unavailable")
            return

        app = Flask(__name__)
        # Silence Flask/werkzeug request logs so they don't clutter Ted's console
        app.logger.disabled = True
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

        api = self._api

        def _authorized():
            if not REMOTE_TOKEN:
                return False
            supplied = (request.headers.get("X-Ted-Token")
                        or request.args.get("token") or "")
            return hmac.compare_digest(str(supplied), str(REMOTE_TOKEN))

        @app.route("/status", methods=["GET"])
        def status():
            if not _authorized():
                return jsonify({"error": "bad token"}), 403
            return jsonify({"status": "ok", "muted": api.muted})

        @app.route("/ask", methods=["GET", "POST"])
        def ask():
            if not _authorized():
                return jsonify({"error": "bad token"}), 403
            data = request.get_json(silent=True) or {}
            text = (data.get("text") or request.args.get("text") or "").strip()
            if not text:
                return jsonify({"error": "no text provided"}), 400

            result = [None]
            done = threading.Event()

            def flow():
                if not api._busy.acquire(timeout=20.0):
                    result[0] = "Ted is still busy with another request."
                    done.set()
                    return
                try:
                    api._respond(text, echo_user=False)
                    result[0] = api.last_reply
                except Exception as e:
                    result[0] = str(e)
                finally:
                    try:
                        api._busy.release()
                    except RuntimeError:
                        pass
                    done.set()

            threading.Thread(target=flow, daemon=True).start()
            if not done.wait(timeout=30.0):
                return jsonify({"error": "Ted did not finish within 30 seconds"}), 504
            return jsonify({"reply": result[0] or ""})

        def run():
            app.run(host=_bind_host(), port=REMOTE_PORT, threaded=True)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        print(f"[remote] authenticated HTTP server listening on LAN port {REMOTE_PORT}")
