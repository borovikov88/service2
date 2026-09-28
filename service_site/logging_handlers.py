"""Fail-open handlers and filters for non-critical, sanitised diagnostics."""

import logging
import logging.handlers
import re
from pathlib import Path


_COMMUNICATION_AVITO_WEBHOOK_RE = re.compile(
    r"(/api/communications/avito/[^/\s?#]+/)[^/\s?#]+(/webhook/?)(?=$|[\s?#])"
)


def _redact_communication_webhook_secret(value):
    if not isinstance(value, str):
        return value
    return _COMMUNICATION_AVITO_WEBHOOK_RE.sub(r"\1[REDACTED]\2", value)


class RedactCommunicationWebhookSecretFilter(logging.Filter):
    """Remove Avito callback secrets before django.request reaches file logs."""

    def filter(self, record):
        record.msg = _redact_communication_webhook_secret(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                _redact_communication_webhook_secret(value) for value in record.args
            )
        elif isinstance(record.args, dict):
            record.args = {
                key: _redact_communication_webhook_secret(value)
                for key, value in record.args.items()
            }
        return True


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
