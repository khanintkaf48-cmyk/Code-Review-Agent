import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from reviewer.config import Config
from reviewer.demo import SAMPLE_DIFF, DemoClient, seed_demo_data
from reviewer.server import ReviewerApp, create_server


class ServerCase(unittest.TestCase):
    token = None
    demo = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = ReviewerApp(Config(data_dir=Path(self.tmp.name)), client=DemoClient(),
                               token=self.token, run_async=False, demo=self.demo)
        self.srv = create_server(self.app, "127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown(); self.srv.server_close(); self.tmp.cleanup()

    def call(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                payload = r.read()
                return r.status, (json.loads(payload) if r.headers["Content-Type"].startswith("application/json") else payload)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


class ApiTests(ServerCase):
    def test_dashboard_page_and_health(self):
        code, page = self.call("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"Reviewer", page)
        self.assertEqual(self.call("GET", "/healthz")[1], {"ok": True})
        self.assertEqual(self.call("GET", "/nope")[0], 404)

    def test_full_loop_over_http(self):
        code, res = self.call("POST", "/api/review", {"diff": SAMPLE_DIFF})
        self.assertEqual(code, 200)
        self.assertEqual(len(res["findings"]), 5)
        fid = next(f["id"] for f in res["findings"] if f["pattern_key"] == "missing-type-hints")
        self.assertEqual(self.call("POST", "/api/feedback",
                                   {"id": fid, "status": "rejected", "reason": "legacy"})[0], 200)
        _, again = self.call("POST", "/api/review", {"diff": SAMPLE_DIFF})
        self.assertEqual(len(again["findings"]), 4)
        self.assertEqual(again["suppressed"][0]["pattern_key"], "missing-type-hints")
        _, stats = self.call("GET", "/api/stats")
        self.assertEqual(stats["rejected"], 1)
        self.assertEqual(len(self.call("GET", "/api/findings?status=rejected")[1]), 1)
        rules = self.call("GET", "/api/rules")[1]
        self.assertIn("legacy", rules[0]["text"])          # reason became a rule
        self.assertEqual(self.call("DELETE", f"/api/rules/{rules[0]['id']}")[0], 200)
        self.assertEqual(self.call("GET", "/api/rules")[1], [])

    def test_rules_crud_and_validation(self):
        self.assertTrue(self.call("POST", "/api/rules", {"text": "Use pathlib"})[1]["added"])
        self.assertEqual(self.call("POST", "/api/rules", {"text": " "})[0], 400)
        self.assertEqual(self.call("POST", "/api/review", {"diff": ""})[0], 400)
        self.assertEqual(self.call("POST", "/api/feedback", {"id": "x", "status": "accepted"})[0], 400)
        self.assertEqual(self.call("POST", "/api/feedback", {"id": 999, "status": "accepted"})[0], 404)
        self.assertEqual(self.call("POST", "/api/review", raw=b"not json")[0], 400)
        self.assertEqual(self.call("GET", "/api/findings?status=bogus")[0], 400)
        self.assertEqual(self.call("DELETE", "/api/rules/abc")[0], 400)
        self.assertEqual(self.call("GET", "/api/sample")[1]["diff"], SAMPLE_DIFF)

    def test_seed_demo_data_only_once(self):
        with self.app.session() as a:
            self.assertTrue(seed_demo_data(a.memory))
            self.assertFalse(seed_demo_data(a.memory))
        stats = self.call("GET", "/api/stats")[1]
        self.assertGreater(stats["rules"], 0)
        self.assertGreater(len(stats["top_mistakes"]), 0)
        self.assertGreater(len(stats["pushback"]), 0)


class ResetTests(ServerCase):
    def test_reset_returns_to_clean_seeded_state(self):
        self.call("POST", "/api/review", {"diff": SAMPLE_DIFF})
        self.assertGreater(self.call("GET", "/api/stats")[1]["open"], 0)
        self.assertEqual(self.call("POST", "/api/reset", {})[0], 200)
        stats = self.call("GET", "/api/stats")[1]
        self.assertEqual(stats["open"], 0)
        self.assertGreater(stats["rules"], 0)          # re-seeded, dashboard not empty

    def test_favicon_is_quiet(self):
        req = urllib.request.Request(self.base + "/favicon.ico")
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 204)


class NonDemoResetTests(ServerCase):
    demo = False

    def test_reset_forbidden_outside_demo(self):
        self.assertEqual(self.call("POST", "/api/reset", {})[0], 403)


class AuthTests(ServerCase):
    token = "hunter2"

    def test_api_requires_token_but_page_does_not(self):
        self.assertEqual(self.call("GET", "/")[0], 200)
        self.assertEqual(self.call("GET", "/api/stats")[0], 401)
        self.assertEqual(self.call("POST", "/api/rules", {"text": "x"})[0], 401)
        self.assertEqual(self.call("GET", "/api/stats", headers={"Authorization": "Bearer nope"})[0], 401)
        self.assertEqual(self.call("GET", "/api/stats", headers={"Authorization": "Bearer hunter2"})[0], 200)


class WebhookTests(ServerCase):
    def test_github_webhook(self):
        payload = json.dumps({"action": "opened", "repository": {"full_name": "a/b"},
                              "pull_request": {"number": 5}}).encode()
        sig = "sha256=" + hmac.new(b"ghsecret", payload, hashlib.sha256).hexdigest()
        hdr = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig}

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_WEBHOOK_SECRET", None)
            self.assertEqual(self.call("POST", "/webhook/github", raw=payload, headers=hdr)[0], 503)

        with mock.patch.dict(os.environ, {"GITHUB_WEBHOOK_SECRET": "ghsecret"}), \
             mock.patch("reviewer.jobs.review_github_pr") as job:
            self.assertEqual(self.call("POST", "/webhook/github", raw=payload,
                                       headers={**hdr, "X-Hub-Signature-256": "sha256=bad"})[0], 401)
            code, body = self.call("POST", "/webhook/github", raw=payload, headers=hdr)
            self.assertEqual(code, 202)
            self.assertEqual(job.call_args[0][1:], ("a/b", 5, True, True))
            ignored = json.dumps({"action": "closed", "repository": {"full_name": "a/b"},
                                  "pull_request": {"number": 5}}).encode()
            sig2 = "sha256=" + hmac.new(b"ghsecret", ignored, hashlib.sha256).hexdigest()
            code, body = self.call("POST", "/webhook/github", raw=ignored,
                                   headers={**hdr, "X-Hub-Signature-256": sig2})
            self.assertEqual((code, "ignored" in body), (200, True))
            self.assertEqual(job.call_count, 1)

    def test_gitlab_webhook(self):
        def event(action, oldrev=None):
            attrs = {"action": action, "iid": 9}
            if oldrev:
                attrs["oldrev"] = oldrev
            return json.dumps({"object_kind": "merge_request", "object_attributes": attrs,
                               "project": {"path_with_namespace": "grp/proj"}}).encode()

        with mock.patch.dict(os.environ, {"GITLAB_WEBHOOK_TOKEN": "tok"}), \
             mock.patch("reviewer.jobs.review_gitlab_mr") as job:
            self.assertEqual(self.call("POST", "/webhook/gitlab", raw=event("open"),
                                       headers={"X-Gitlab-Token": "wrong"})[0], 401)
            ok = {"X-Gitlab-Token": "tok"}
            self.assertEqual(self.call("POST", "/webhook/gitlab", raw=event("open"), headers=ok)[0], 202)
            self.assertEqual(self.call("POST", "/webhook/gitlab", raw=event("update"), headers=ok)[0], 200)  # title edit
            self.assertEqual(self.call("POST", "/webhook/gitlab", raw=event("update", "abc"), headers=ok)[0], 202)
            self.assertEqual(job.call_count, 2)
            self.assertEqual(job.call_args[0][1:], ("grp/proj", 9, True, True))

    def test_slack_endpoints(self):
        def slack_call(path, form):
            body = urlencode(form).encode()
            ts = str(int(time.time()))
            sig = "v0=" + hmac.new(b"ss", b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()
            return self.call("POST", path, raw=body, headers={
                "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
                "Content-Type": "application/x-www-form-urlencoded"})

        with mock.patch.dict(os.environ, {"SLACK_SIGNING_SECRET": "ss"}):
            code, body = slack_call("/slack/commands", {"text": "teach Prefer composition"})
            self.assertEqual(code, 200)
            self.assertIn("Rule added", body["text"])
            code, res = self.call("POST", "/api/review", {"diff": SAMPLE_DIFF})
            fid = res["findings"][0]["id"]
            payload = {"user": {"name": "dev"}, "actions": [{"action_id": "fb_accepted", "value": str(fid)}]}
            code, body = slack_call("/slack/interactions", {"payload": json.dumps(payload)})
            self.assertIn("accepted", body["text"])
            # forged request
            self.assertEqual(self.call("POST", "/slack/commands", raw=b"text=stats",
                                       headers={"X-Slack-Request-Timestamp": str(int(time.time())),
                                                "X-Slack-Signature": "v0=forged"})[0], 401)


if __name__ == "__main__":
    unittest.main()
