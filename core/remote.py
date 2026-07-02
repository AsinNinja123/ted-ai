"""
core/remote.py — Simple HTTP endpoint for remote Ted control.

Exposes Ted over a local network so iOS Shortcuts or curl can talk to Ted.

Endpoints:
    POST /ask   {"text": "..."}  →  {"reply": "..."}
    GET  /status                 →  {"status": "ok", "muted": bool}

Usage in hud.py TedApi.start():
    from core.remote import RemoteServer
    RemoteServer(api).start()
"""

import threading
import time
import logging

try:
    from config import REMOTE_PORT
except Exception:
    REMOTE_PORT = 5150


class RemoteServer:
    def __init__(self, api):
        self._api = api

    def start(self):
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

        @app.route("/status", methods=["GET"])
        def status():
            return jsonify({"status": "ok", "muted": api.muted})

        @app.route("/ask", methods=["POST"])
        def ask():
            data = request.get_json(silent=True) or {}
            text = (data.get("text") or "").strip()
            if not text:
                return jsonify({"error": "no text provided"}), 400

            result = [None]
            done = threading.Event()

            def flow():
                for _ in range(400):
                    if api._busy.acquire(blocking=False):
                        break
                    time.sleep(0.05)
                else:
                    api._busy.acquire()
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
            done.wait(timeout=30.0)
            return jsonify({"reply": result[0] or ""})

        def run():
            app.run(host="0.0.0.0", port=REMOTE_PORT, threaded=True)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        print(f"[remote] HTTP server listening on port {REMOTE_PORT}")
