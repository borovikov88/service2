import logging
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import SimpleTestCase

from service_site.logging_handlers import FailOpenWatchedFileHandler


class FailOpenWatchedFileHandlerTests(SimpleTestCase):
    def test_creates_missing_parent_directory_before_first_write(self):
        with TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "nested" / "django-request-error.log"
            handler = FailOpenWatchedFileHandler(
                str(log_path),
                encoding="utf-8",
                delay=True,
            )
            try:
                record = logging.LogRecord(
                    name="django.request",
                    level=logging.ERROR,
                    pathname=__file__,
                    lineno=1,
                    msg="safe test error",
                    args=(),
                    exc_info=None,
                )
                handler.emit(record)
            finally:
                handler.close()

            self.assertTrue(log_path.exists())
            self.assertEqual(log_path.parent.stat().st_mode & 0o777, 0o700)
            self.assertIn("safe test error", log_path.read_text(encoding="utf-8"))
