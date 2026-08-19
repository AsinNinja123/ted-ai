"""dashboard — the Flask app behind Ted's Memory, Notebook and Diagnostics panels.

# =============================================================================
#  READING THIS FILE                     The Ted Code Book — Chapter 15
# =============================================================================
#
#  WHAT THIS FILE IS
#      Empty on purpose, apart from this note.
#
#      A file named __init__.py is what tells Python that a folder is a
#      "package" — something you can write `from dashboard import db` about. It
#      does not have to contain anything, and this one deliberately does not:
#      putting code here would make it run on every import of anything in the
#      folder, which is a surprising place for work to happen.
#
#      The real files are:
#          dashboard/app.py         the web server and its routes      §15.1-15.2
#          dashboard/db.py          schema, table registry, audit      §15.3-15.4
#          dashboard/index.html     the Memory page
#          dashboard/notebook.html  the Notebook page
#          dashboard/diagnostics.html  the per-turn Diagnostics page
# =============================================================================
"""
