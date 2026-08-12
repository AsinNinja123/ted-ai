"""python -m dashboard [port]"""
import sys
import threading
import webbrowser

from dashboard.app import main

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5175
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    main(port)
