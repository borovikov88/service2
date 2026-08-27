import logging
from unittest.mock import patch

from django.test import SimpleTestCase

from service_site.logging_handlers import FailOpenWatchedFileHandler


class FailOpenWatchedFileHandlerTests(SimpleTestCase):
    def test_oserror_from_watched_file_emit_does_not_escape(self):
        handler = FailOpenWatchedFileHandler("unused.log", delay=True)
        record = logging.LogRecord(
            "pool_service.finance_imports.odata_unified_sync",
            logging.ERROR,
            __file__,
            1,
            "safe diagnostic",
            (),
            None,
        )

        with patch("logging.handlers.WatchedFileHandler.emit", side_effect=OSError("unavailable")):
            self.assertIsNone(handler.emit(record))
