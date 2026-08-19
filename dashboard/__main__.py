"""python -m dashboard [port]"""

# =============================================================================
#  READING THIS FILE                     The Ted Code Book — Chapter 15 (§15.1)
# =============================================================================
#
#  WHAT THIS FILE IS
#      Eleven lines that let you run the dashboard on its own, without Ted:
#
#          python -m dashboard          (then open http://127.0.0.1:5175)
#
#      A file named __main__.py inside a folder is what Python runs when you
#      say `python -m <foldername>`. That is the entire trick.
#
#      The dashboard normally starts inside Ted (see hud.py), on a background
#      thread. This file is the standalone door — useful when you want to read
#      or edit memory while Ted is closed.
# =============================================================================
import sys
import threading
import webbrowser

from dashboard.app import main

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5175
    threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    main(port)
