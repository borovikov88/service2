"""Protocol and security tests using inert fixtures; no credentials or network."""
import base64
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("direct_review_script", ROOT / ".github/scripts/review_direct_pr.py")
review = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(review)
BASE, HEAD, OLD = "a" * 40, "b" * 40, "c" * 40
ENV = {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "TRUSTED_SHA": BASE}


def pr():
    return {"number": 90, "title": "Fix check", "body": "Ensure empty input is rejected; regression test required.",
            "state": "open", "draft": False, "user": {"login": review.OWNER}, "changed_files": 1,
            "base": {"sha": BASE, "ref": "main", "repo": {"full_name": review.REPOSITORY}},
            "head": {"sha": HEAD, "ref": "codex/fix", "repo": {"full_name": review.REPOSITORY}}}


def accepted():
    return {"decision": "accepted", "sufficient_context": True, "summary": "Reviewed source.", "findings": []}


def artifact(result=None):
    return {"run": review.run_binding(ENV), "model": review.MODEL,
            "binding": review.binding(pr()), "review": result or accepted()}


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def request(self, path, data=None):
        self.calls.append((path, data))
        if not self.responses:
            raise AssertionError("Unexpected API call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def writes(self):
        return [call for call in self.calls if call[1] is not None]


def bot_row(state="APPROVED", row_id=1, head=HEAD, body=""):
    return {"id": row_id, "user": {"login": review.BOT}, "state": state,
            "commit_id": head, "submitted_at": f"2026-09-05T12:00:{row_id:02}Z", "body": body}


class DirectReviewTests(unittest.TestCase):
    def test_missing_token_fails_without_network(self):
        with patch.object(review.urllib.request, "build_opener") as network:
            for token in (None, "", "  "):
                with self.assertRaises(review.Blocked):
                    review.Client(token)
            network.assert_not_called()

    def test_bad_credential_response_makes_no_write(self):
        client = FakeClient([review.Blocked("Unauthorized")])
        with self.assertRaises(review.Blocked):
            review.publish(client, artifact(), ENV, 90)
        self.assertEqual(client.writes, [])

    def test_wrong_identity_cannot_publish(self):
        for login in (review.OWNER, "another-bot"):
            client = FakeClient([pr(), {"login": login}])
            with self.assertRaises(review.Blocked):
                review.publish(client, artifact(), ENV, 90)
            self.assertEqual(client.writes, [])

    def test_fork_draft_and_missing_task_are_rejected(self):
        samples = []
        item = pr(); item["head"]["repo"]["full_name"] = "attacker/service2"; samples.append(item)
        item = pr(); item["draft"] = True; samples.append(item)
        item = pr(); item["body"] = ""; samples.append(item)
        item = pr(); item["head"]["sha"] = "bad"; samples.append(item)
        item = pr(); item["user"]["login"] = "attacker"; samples.append(item)
        for item in samples:
            with self.assertRaises(review.Blocked):
                review.binding(item)

    def test_moved_head_base_or_task_blocks_publication(self):
        for kind in ("head", "base", "body"):
            moved = pr()
            if kind == "body":
                moved["body"] += " Changed criteria"
            else:
                moved[kind]["sha"] = OLD
            client = FakeClient([pr(), {"login": review.BOT}, [], moved])
            with self.assertRaises(review.Blocked):
                review.publish(client, artifact(), ENV, 90)
            self.assertEqual(client.writes, [])

    def test_wrong_run_artifact_is_rejected_before_network(self):
        item = artifact(); item["run"]["GITHUB_RUN_ID"] = "456"
        client = FakeClient([])
        with self.assertRaises(review.Blocked):
            review.publish(client, item, ENV, 90)
        self.assertEqual(client.calls, [])

    def test_exact_head_approval_is_posted_only_after_last_recheck(self):
        client = FakeClient([pr(), {"login": review.BOT}, [], pr(), bot_row()])
        self.assertIn("APPROVED", review.publish(client, artifact(), ENV, 90))
        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.writes[0][1]["commit_id"], HEAD)
        self.assertEqual(client.writes[0][1]["event"], "APPROVE")
        self.assertIsNone(client.calls[-2][1])

    def test_insufficient_context_requests_changes_instead_of_approval(self):
        result = accepted(); result.update(decision="needs_context", sufficient_context=False)
        client = FakeClient([pr(), {"login": review.BOT}, [], pr(), bot_row("CHANGES_REQUESTED")])
        review.publish(client, artifact(result), ENV, 90)
        self.assertEqual(client.writes[0][1]["event"], "REQUEST_CHANGES")

    def test_same_result_rerun_is_idempotent(self):
        item = artifact()
        marker = "service2-review:" + hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
        client = FakeClient([pr(), {"login": review.BOT}, [bot_row(body=marker)]])
        self.assertIn("already", review.publish(client, item, ENV, 90))
        self.assertEqual(client.writes, [])

    def test_paginated_later_dismissal_invalidates_approval(self):
        comments = [{"user": {"login": "someone"}, "state": "COMMENTED"}] * 99
        client = FakeClient([[bot_row()] + comments, [bot_row("DISMISSED", 2)]])
        rows = review.pages(client, "/pulls/90/reviews")
        self.assertEqual(review.latest_bot_review(rows)["state"], "DISMISSED")
        self.assertTrue(client.calls[-1][0].endswith("page=2"))
        self.assertEqual(review.latest_bot_review([bot_row(), bot_row("CHANGES_REQUESTED", 2)])["state"],
                         "CHANGES_REQUESTED")

    def test_broken_bot_review_is_not_silently_accepted(self):
        row = bot_row(); del row["submitted_at"]
        with self.assertRaises(review.Blocked):
            review.latest_bot_review([row])

    def test_invalid_acceptance_and_empty_changes_are_rejected(self):
        for result in (
            dict(accepted(), sufficient_context=False),
            dict(accepted(), findings=[{"path": "x.py", "detail": "Defect"}]),
            dict(accepted(), decision="changes_requested"),
            dict(accepted(), extra=True),
        ):
            with self.assertRaises(review.Blocked):
                review.validate_review(result)

    def test_structured_response_and_refusal(self):
        response = {"status": "completed", "output": [{"type": "message", "role": "assistant",
                    "status": "completed", "content": [{"type": "output_text", "text": json.dumps(accepted())}]}]}
        client = FakeClient([response])
        self.assertEqual(review.model_review(client, {"changes": []}), accepted())
        request = client.calls[0][1]
        self.assertFalse(request["store"])
        self.assertNotIn("tools", request)
        self.assertEqual(request["text"]["format"]["type"], "json_schema")
        for broken in (
            {"status": "incomplete", "output": []},
            {"status": "completed", "output": [{"type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "refusal", "refusal": "No"}]}]},
            {"status": "completed", "output": []},
        ):
            with self.assertRaises(review.Blocked):
                review.model_review(FakeClient([broken]), {})

    def source_client(self, raw=b"print('ok')\n", mode="100644", size=None):
        sha = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        return FakeClient([
            {"truncated": False, "tree": [{"path": "file.py", "mode": mode, "type": "blob", "sha": sha}]},
            {"type": "file", "encoding": "base64", "size": len(raw) if size is None else size,
             "content": base64.b64encode(raw).decode()},
        ])

    def test_source_rejects_symlink_binary_oversize_and_partial(self):
        self.assertIn("print", review.source(self.source_client(), "file.py", HEAD))
        for client in (self.source_client(mode="120000"), self.source_client(raw=b"\0"),
                       self.source_client(size=review.MAX_FILE + 1), self.source_client(size=99),
                       FakeClient([{"truncated": True, "tree": []}]),
                       FakeClient([{"truncated": False, "tree": []}])):
            with self.assertRaises(review.Blocked):
                review.source(client, "file.py", HEAD)

    def test_source_rejects_tree_content_mismatch(self):
        client = self.source_client()
        client.responses[0]["tree"][0]["sha"] = OLD
        with self.assertRaises(review.Blocked):
            review.source(client, "file.py", HEAD)

    def test_file_list_is_complete_and_bounded(self):
        for files in ([], [{"filename": "x", "status": "modified"}] * 41):
            client = FakeClient([files])
            with self.assertRaises(review.Blocked):
                review.build_bundle(client, pr())

    def test_bundle_uses_merge_base_and_refuses_missing_context(self):
        client = FakeClient([[{"filename": "x.py", "status": "modified"}], {"merge_base_commit": {"sha": OLD}}])
        with patch.object(review, "source", return_value="source text\n") as source, \
             patch.object(review, "tree_entry", return_value={"mode": "100644"}):
            result = review.build_bundle(client, pr())
            self.assertEqual(len(result["changes"]), 1)
            self.assertIn(unittest.mock.call(client, "x.py", OLD), source.call_args_list)
            self.assertIn(unittest.mock.call(client, "x.py", HEAD), source.call_args_list)
        client = FakeClient([[{"filename": "x.py", "status": "modified"}], {"merge_base_commit": {"sha": OLD}}])
        with patch.object(review, "source", side_effect=review.Blocked("Missing source")), \
             patch.object(review, "tree_entry", return_value={"mode": "100644"}):
            with self.assertRaises(review.Blocked):
                review.build_bundle(client, pr())

    def test_mode_only_change_is_visible_and_added_collision_is_blocked(self):
        responses = [[{"filename": "x.py", "status": "modified"}], {"merge_base_commit": {"sha": OLD}}]
        with patch.object(review, "source", return_value="unchanged text"), \
             patch.object(review, "tree_entry", side_effect=[{"mode": "100755"}, {"mode": "100644"}]):
            bundle = review.build_bundle(FakeClient(responses), pr())
            change = bundle["changes"][0]
            self.assertEqual(change["diff"], "")
            self.assertEqual((change["before_mode"], change["after_mode"]), ("100755", "100644"))
        responses = [[{"filename": "x.py", "status": "added"}], {"merge_base_commit": {"sha": OLD}}]
        with patch.object(review, "source", return_value="unchanged text"), \
             patch.object(review, "tree_entry", return_value={"mode": "100644"}):
            with self.assertRaises(review.Blocked):
                review.build_bundle(FakeClient(responses), pr())

    def test_manual_event_requires_owner_and_main(self):
        with tempfile.TemporaryDirectory() as temp:
            event = Path(temp) / "event.json"; event.write_text(json.dumps({"inputs": {"pr_number": "90"}}))
            env = dict(ENV, GITHUB_EVENT_PATH=str(event), GITHUB_REPOSITORY=review.REPOSITORY,
                       GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_REF="refs/heads/main",
                       GITHUB_ACTOR=review.OWNER, GITHUB_TRIGGERING_ACTOR=review.OWNER)
            self.assertEqual(review.event_number(env), 90)
            for key, value in (("GITHUB_REF", "refs/heads/feature"), ("GITHUB_ACTOR", "github-actions[bot]"),
                               ("GITHUB_TRIGGERING_ACTOR", "outsider")):
                with self.assertRaises(review.Blocked):
                    review.event_number(dict(env, **{key: value}))

    def test_readonly_connection_check_reports_presence_not_secret(self):
        client = FakeClient([{"login": review.BOT}, {"full_name": review.REPOSITORY, "permissions": {"push": True}}])
        env = dict(ENV, GITHUB_REPOSITORY=review.REPOSITORY, GITHUB_REF="refs/heads/main",
                   SERVICE2_REVIEW_TOKEN="secret-review", OPENAI_API_KEY="secret-model", GITHUB_SHA=BASE)
        with patch.object(review, "Client", return_value=client), patch("builtins.print") as output:
            review.check_connection(env)
        self.assertEqual(client.writes, [])
        self.assertNotIn("secret-", str(output.call_args_list))
        self.assertIn("available", str(output.call_args_list))

    def test_api_redirect_cannot_forward_credentials(self):
        with self.assertRaises(review.Blocked):
            review.NoRedirect().redirect_request(None, None, 302, "", {}, "https://attacker.invalid")

    def test_workflow_separates_tokens_and_never_checks_out_pr_head(self):
        workflow = (ROOT / ".github/workflows/direct-pr-review.yml").read_text()
        reviewer = workflow.split("  review:\n", 1)[1].split("  publish:\n", 1)[0]
        publisher = workflow.split("  publish:\n", 1)[1]
        self.assertNotIn("SERVICE2_REVIEW_TOKEN", reviewer)
        self.assertNotIn("OPENAI_API_KEY", publisher)
        self.assertNotIn("head.sha", workflow)
        self.assertNotIn("pip install", workflow)
        self.assertNotIn("run-id:", workflow)
        self.assertNotIn("pull-requests: write", workflow)
        self.assertIn("ref: ${{ env.TRUSTED_SHA }}", reviewer)
        self.assertIn("ref: ${{ env.TRUSTED_SHA }}", publisher)
        self.assertIn("environment: review", publisher)

    def test_review_artifact_paths_are_scoped_to_runner_steps(self):
        workflow = (ROOT / ".github/workflows/direct-pr-review.yml").read_text()
        workflow_settings, jobs = workflow.split("\njobs:\n", 1)
        reviewer = jobs.split("  review:\n", 1)[1].split("  publish:\n", 1)[0]
        publisher = jobs.split("  publish:\n", 1)[1]
        # runner is unavailable while GitHub evaluates workflow/job env. The
        # original placement prevented every job, including connection, starting.
        self.assertNotIn("runner.", workflow_settings)
        for job in (reviewer, publisher):
            self.assertNotIn("runner.", job.split("    steps:\n", 1)[0])

        artifact_dir = "${{ runner.temp }}/independent-review"
        artifact_path = artifact_dir + "/result.json"
        review_step = reviewer.split("      - name: Review inert PR source through APIs\n", 1)[1].split("      - name:", 1)[0]
        publish_step = publisher.split("      - name: Publish exact-head review using independent identity\n", 1)[1]
        for step in (review_step, publish_step):
            self.assertIn("        env:\n          REVIEW_ARTIFACT: " + artifact_path + "\n", step)
        # Producer, uploaded file, downloaded directory and publisher must agree.
        self.assertIn("          path: " + artifact_path + "\n", reviewer)
        self.assertIn("          path: " + artifact_dir + "\n", publisher)


if __name__ == "__main__":
    unittest.main()
