"""core/logs.py — Ted's rotating error log.

Only ERROR-level messages are written (print() handles debug noise).
5 MB × 3 files so the disk never fills up.
"""

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
