from pathlib import Path
from unittest import TestCase


class Dev0013AuditReportTests(TestCase):
    def test_report_lists_every_older_reference_and_does_not_claim_completion(self):
        report = (
            Path(__file__).resolve().parents[2]
            / "docs"
            / "dev-0013-legacy-task-audit.md"
        ).read_text(encoding="utf-8")

        for task_number in range(1, 13):
            row_prefix = f"| DEV-{task_number:04d} | недостаточно данных |"
            self.assertIn(row_prefix, report)

        self.assertIn("Ни одна задача не помечена выполненной", report)
        self.assertIn("DevelopmentTask", report)
        self.assertIn("iterations", report)
        self.assertIn("events", report)
        self.assertIn("GitHub API", report)
        self.assertIn("production", report)
