import hashlib
import hmac
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from reviewer import gitlab_integration as gl
from reviewer import slack_integration as slack
from reviewer.demo import SAMPLE_DIFF, DemoClient
from reviewer.agent import ReviewAgent
from reviewer.config import Config
from reviewer.diff_utils import parse_diff
from reviewer.memory import Memory


def sign(secret, ts, body):
    return "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body,
                            hashlib.sha256).hexdigest()


class SlackTests(unittest.TestCase):
    def test_signature_ok_bad_and_replay(self):
        body, now = b"text=stats", time.time()
        ts = str(int(now))
        good = sign("s3cret", ts, body)
        self.assertTrue(slack.verify_signature("s3cret", ts, body, good, now=now))
        self.assertFalse(slack.verify_signature("s3cret", ts, body, good[:-1] + "0", now=now))
        self.assertFalse(slack.verify_signature("other", ts, body, good, now=now))
        self.assertFalse(slack.verify_signature("s3cret", ts, body, good, now=now + 1000))  # replay
        self.assertFalse(slack.verify_signature("s3cret", "abc", body, good, now=now))
        self.assertFalse(slack.verify_signature("", ts, body, good, now=now))

    def make_agent(self):
        return ReviewAgent(Config(), Memory(":memory:"), DemoClient())

    def test_commands(self):
        a = self.make_agent()
        self.assertIn("Rule added", slack.handle_command(a, "teach Use pathlib"))
        self.assertIn("Use pathlib", slack.handle_command(a, "rules"))
        self.assertIn("Learned so far", slack.handle_command(a, "stats"))
        self.assertIn("Usage", slack.handle_command(a, "feedback abc"))
        self.assertIn("Reviewer commands", slack.handle_command(a, ""))
        res = a.review(SAMPLE_DIFF)
        fid = res.findings[0]["id"]
        self.assertIn("marked rejected", slack.handle_command(a, f"feedback {fid} rejected too noisy"))
        self.assertEqual(a.memory.get_finding(fid)["status"], "rejected")

    def test_button_reject_does_not_create_rule(self):
        a = self.make_agent()
        fid = a.review(SAMPLE_DIFF).findings[0]["id"]
        payload = {"user": {"name": "dev"}, "actions": [{"action_id": "fb_rejected", "value": str(fid)}]}
        self.assertIn("rejected", slack.handle_interaction(a, payload))
        self.assertEqual(a.memory.list_rules(), [])   # no junk "do not flag" rule from a bare click
        self.assertEqual(slack.handle_interaction(a, {"actions": [{"action_id": "x", "value": "1"}]}),
                         "Unknown action.")

    def test_blocks_have_buttons_and_escape(self):
        a = self.make_agent()
        res = a.review(SAMPLE_DIFF)
        res.findings[0]["message"] = "<script>&"
        blocks = slack.review_blocks(res, title="a <b> title")
        text = str(blocks)
        self.assertIn("fb_accepted", text)
        self.assertIn("&lt;script&gt;&amp;", text)
        self.assertLessEqual(len(blocks), 50)

    def test_notify_noop_without_webhook(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(slack.notify_review(SimpleNamespace(review_id="x", findings=[],
                                                                 suppressed=[], summary="")))


class DemoModelTests(unittest.TestCase):
    def test_demo_is_honest_about_other_code(self):
        a = ReviewAgent(Config(), Memory(":memory:"), DemoClient())
        res = a.review('Print ("Hello i  am intkah")')
        self.assertEqual(res.findings, [])
        self.assertIn("Demo mode", res.summary)
        self.assertEqual(len(a.review(SAMPLE_DIFF).findings), 5)   # sample still works

    def test_memory_mentioning_sample_path_does_not_confuse_demo_model(self):
        """Regression: after a rejection the sample's path appears in the prompt's memory
        section; other code must still get the honest 'demo only knows the sample' answer."""
        a = ReviewAgent(Config(), Memory(":memory:"), DemoClient())
        first = a.review(SAMPLE_DIFF)
        a.memory.set_status(first.findings[0]["id"], "rejected", "we use an ORM")
        res = a.review('Print ("Hello")')
        self.assertEqual(res.findings, [])
        self.assertIn("Demo mode", res.summary)


class GitLabTests(unittest.TestCase):
    CHANGES = [
        {"old_path": "a.py", "new_path": "a.py", "new_file": True, "deleted_file": False,
         "diff": "@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n"},
        {"old_path": "b.py", "new_path": "b.py", "new_file": False, "deleted_file": True, "diff": "@@ -1 +0,0 @@\n-z\n"},
        {"old_path": "c.py", "new_path": "d.py", "new_file": False, "deleted_file": False,
         "diff": "@@ -1,2 +1,2 @@\n a\n-b\n+c\n"},
    ]

    def test_changes_to_diff_parses(self):
        diff = gl.changes_to_diff(self.CHANGES)
        self.assertNotIn("b.py", diff)   # deleted files skipped
        p = parse_diff(diff)
        self.assertEqual(sorted(p.files), ["a.py", "d.py"])
        self.assertEqual(p.added_lines["a.py"], {1, 2})
        self.assertEqual(p.added_lines["d.py"], {2})

    def test_post_review_inline_then_fallback(self):
        findings = [{"id": 1, "file": "a.py", "line": 2, "severity": "major", "category": "bug",
                     "message": "m1", "suggestion": "s"},
                    {"id": 2, "file": "a.py", "line": None, "severity": "nit", "category": "x",
                     "message": "m2", "suggestion": None}]
        mr = {"diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": "h"}}
        calls = []

        def fake_post(url, json=None, headers=None, timeout=None):
            calls.append((url, json))
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None)

        with mock.patch.dict("os.environ", {"GITLAB_TOKEN": "t"}), \
             mock.patch("reviewer.gitlab_integration.requests.post", fake_post):
            gl.post_review("grp/proj", 7, "summary", findings, mr)
        self.assertTrue(calls[0][0].endswith("/projects/grp%2Fproj/merge_requests/7/discussions"))
        self.assertEqual(calls[0][1]["position"]["new_line"], 2)
        self.assertTrue(calls[1][0].endswith("/notes"))
        self.assertIn("m2", calls[1][1]["body"])   # unplaceable finding went into the note

    def test_requires_token(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(RuntimeError):
                gl.fetch_mr("a/b", 1)


if __name__ == "__main__":
    unittest.main()
