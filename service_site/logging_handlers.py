"""Fail-open handlers for non-critical, already-sanitised diagnostics."""

import logging.handlers


class FailOpenWatchedFileHandler(logging.handlers.WatchedFileHandler):
    """Never let an unavailable diagnostics sink affect a sync result."""

    def emit(self, record):
        try:
            super().emit(record)
        except OSError:
            # Do not create a second failure or disclose the original record.
            return
