"""core/logs.py — Ted's rotating error log.

Only ERROR-level messages are written (print() handles debug noise).
5 MB × 3 files so the disk never fills up.
"""

# =============================================================================
#  READING THIS FILE            The Ted Code Book — Chapter 29 (§29.5)
# =============================================================================
#
#  WHAT THIS FILE IS
#      The rotating error log. Nineteen lines, and it exists because of the single
#      most expensive bug in this project's history.
#
#      Fact extraction was dead for five weeks and nobody noticed, because the
#      exception that killed it was printed to stdout — into a terminal nobody was
#      reading — instead of being logged. Real failures now go to ted_errors.log.
#      print() is for noise; error_log is for things that are actually wrong. §34.
#
# =============================================================================

import logging
import logging.handlers

from core.paths import LOG_PATH

_handler = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

error_log = logging.getLogger("ted")
error_log.setLevel(logging.ERROR)
error_log.addHandler(_handler)
