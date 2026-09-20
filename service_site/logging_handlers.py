"""Fail-open handlers for non-critical, already-sanitised diagnostics."""

import logging.handlers
from pathlib import Path


class FailOpenWatchedFileHandler(logging.handlers.WatchedFileHandler):
    """Never let an unavailable diagnostics sink affect application requests."""

    def _open(self):
        Path(self.baseFilename).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return super()._open()

    def emit(self, record):
        try:
            super().emit(record)
        except OSError:
            # Do not create a second failure or disclose the original record.
            return
