from django.test import SimpleTestCase

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / ".github/scripts/verify_deploy_review.py"
SPEC = spec_from_file_location("verify_deploy_review", SCRIPT)
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def review(review_id, state, login="reviewer", commit="head"):
    return {"id": review_id, "state": state, "user": {"login": login}, "commit_id": commit}


class DeployReviewPolicyTests(SimpleTestCase):
    def accepted(self, pages):
        return MODULE.has_current_independent_approval(pages, "author", "head")

    def test_accepts_current_independent_approval(self):
        self.assertTrue(self.accepted([[review(1, "APPROVED")]]))

    def test_rejects_approval_followed_by_changes_requested(self):
        self.assertFalse(self.accepted([[review(1, "APPROVED"), review(2, "CHANGES_REQUESTED")]]))

    def test_rejects_dismissed_approval(self):
        self.assertFalse(self.accepted([[review(1, "APPROVED"), review(2, "DISMISSED")]]))

    def test_rejects_old_head_approval(self):
        self.assertFalse(self.accepted([[review(1, "APPROVED", commit="old")]]))

    def test_reads_all_paginated_reviews(self):
        first_page = [review(number, "COMMENTED") for number in range(1, 101)]
        self.assertTrue(self.accepted([first_page, [review(101, "APPROVED")]]))

    def test_fails_closed_for_malformed_pages(self):
        self.assertFalse(self.accepted({"not": "pages"}))
        self.assertFalse(self.accepted([[{"state": "APPROVED", "commit_id": "head"}]]))
