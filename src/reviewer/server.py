"""Dashboard + JSON API + webhooks in one stdlib HTTP server (no extra dependencies).

Routes
  GET    /                        dashboard
  GET    /healthz                 liveness
  GET    /api/stats|findings|patterns|rules
  POST   /api/review              {diff, title?, description?}
  POST   /api/feedback            {id, status, reason?}
  POST   /api/rules               {text, category?, language?}
  DELETE /api/rules/<id>
  POST   /webhook/github          PR events        (GITHUB_WEBHOOK_SECRET)
  POST   /webhook/gitlab          MR events        (GITLAB_WEBHOOK_TOKEN)
  POST   /slack/commands          slash command    (SLACK_SIGNING_SECRET)
  POST   /slack/interactions      button clicks    (SLACK_SIGNING_SECRET)

Set REVIEWER_DASHBOARD_TOKEN to require `Authorization: Bearer <token>` on /api/*.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import webbrowser
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import jobs
from . import slack_integration as slack
from .agent import ReviewAgent
from .config import Config
from .memory import VALID_STATUSES, Memory

WEB_DIR = Path(__file__).parent / "web"
MAX_BODY = 5 * 1024 * 1024


class ReviewerApp:
    def __init__(self, config: Config | None = None, client=None, token: str | None = None,
                 run_async: bool = True, demo: bool = False, verbose: bool = False):
        self.config = config or Config()
        self.client = client
        self.token = token if token is not None else os.getenv("REVIEWER_DASHBOARD_TOKEN")
        self.run_async = run_async
        self.demo = demo
        self.verbose = verbose
        self.config.data_dir.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def session(self):
        """A fresh agent + DB connection per request (sqlite connections aren't shared)."""
        agent = ReviewAgent(self.config, Memory(self.config.db_path), self.client)
        try:
            yield agent
        finally:
            agent.memory.close()

    def background(self, fn, *args) -> None:
        """Run a webhook job without blocking the HTTP response (webhooks time out fast)."""
        def runner():
            try:
                with self.session() as agent:
                    fn(agent, *args)
            except Exception as e:  # noqa: BLE001
                print(f"[error] background job failed: {e}")

        if self.run_async:
            threading.Thread(target=runner, daemon=True).start()
        else:
            runner()


def _result_dict(res) -> dict:
    return {"review_id": res.review_id, "summary": res.summary, "findings": res.findings,
            "suppressed": res.suppressed, "truncated": res.truncated}


def make_handler(app: ReviewerApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ReviewerAgent/0.2"

        def log_message(self, fmt, *args):  # quiet unless --verbose
            if app.verbose:
                super().log_message(fmt, *args)

        # ------------------------------------------------------------ helpers
        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, default=str).encode())

        def _raw_body(self) -> bytes | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self._json(413, {"error": "request body too large"})
                return None
            return self.rfile.read(length) if length else b""

        def _json_body(self, raw: bytes) -> dict | None:
            try:
                data = json.loads(raw or b"{}")
                if not isinstance(data, dict):
                    raise ValueError
                return data
            except ValueError:
                self._json(400, {"error": "body must be a JSON object"})
                return None

        def _authorized(self) -> bool:
            if not app.token:
                return True
            supplied = (self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
                        or self.headers.get("X-Token", ""))
            return hmac.compare_digest(supplied, app.token)

        # -------------------------------------------------------------- verbs
        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                page = (WEB_DIR / "index.html").read_bytes()
                return self._send(200, page, "text/html; charset=utf-8")
            if url.path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if url.path == "/healthz":
                return self._json(200, {"ok": True})
            if not url.path.startswith("/api/"):
                return self._json(404, {"error": "not found"})
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            qs = parse_qs(url.query)
            try:
                with app.session() as agent:
                    return self._api_get(agent, url.path, qs)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)})

        def _api_get(self, agent, path, qs):
            m = agent.memory
            if path == "/api/stats":
                s = m.stats()
                s["top_mistakes"] = [dict(r) for r in m.frequent_mistakes(6)]
                s["pushback"] = [dict(r) for r in m.recent_pushback(6)]
                s["model"] = "demo (offline)" if app.demo else app.config.model
                s["demo"] = app.demo
                return self._json(200, s)
            if path == "/api/findings":
                status = (qs.get("status") or [None])[0]
                if status and status not in VALID_STATUSES | {"open"}:
                    return self._json(400, {"error": "bad status"})
                limit = min(int((qs.get("limit") or ["100"])[0]), 500)
                return self._json(200, m.list_findings(status, limit))
            if path == "/api/sample":
                from .demo import SAMPLE_DIFF
                return self._json(200, {"diff": SAMPLE_DIFF})
            if path == "/api/patterns":
                return self._json(200, m.list_patterns())
            if path == "/api/rules":
                return self._json(200, [dict(r) for r in m.list_rules()])
            return self._json(404, {"error": "not found"})

        def do_DELETE(self):  # noqa: N802
            path = urlparse(self.path).path
            if not path.startswith("/api/rules/"):
                return self._json(404, {"error": "not found"})
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            rule_id = path.rsplit("/", 1)[1]
            if not rule_id.isdigit():
                return self._json(400, {"error": "bad id"})
            with app.session() as agent:
                ok = agent.memory.delete_rule(int(rule_id))
            return self._json(200 if ok else 404, {"deleted": ok})

        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path
            raw = self._raw_body()
            if raw is None:
                return
            try:
                if path.startswith("/api/"):
                    if not self._authorized():
                        return self._json(401, {"error": "unauthorized"})
                    return self._api_post(path, raw)
                if path == "/webhook/github":
                    return self._github_webhook(raw)
                if path == "/webhook/gitlab":
                    return self._gitlab_webhook(raw)
                if path in ("/slack/commands", "/slack/interactions"):
                    return self._slack(path, raw)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)})
            return self._json(404, {"error": "not found"})

        # ---------------------------------------------------------- /api POST
        def _api_post(self, path: str, raw: bytes):
            data = self._json_body(raw)
            if data is None:
                return
            if path == "/api/reset":
                if not app.demo:
                    return self._json(403, {"error": "reset is only available in demo mode"})
                from .demo import seed_demo_data
                with app.session() as agent:
                    agent.memory.reset()
                    seed_demo_data(agent.memory)
                return self._json(200, {"ok": True})
            with app.session() as agent:
                if path == "/api/review":
                    diff = data.get("diff", "")
                    if not isinstance(diff, str) or not diff.strip():
                        return self._json(400, {"error": "diff is required"})
                    res = agent.review(diff, data.get("title"), data.get("description"))
                    return self._json(200, _result_dict(res))
                if path == "/api/feedback":
                    status, fid = data.get("status"), data.get("id")
                    if status not in VALID_STATUSES or not isinstance(fid, int):
                        return self._json(400, {"error": "need integer id and a valid status"})
                    reason = (data.get("reason") or "").strip() or None
                    ok = agent.memory.set_status(fid, status, reason)
                    return self._json(200 if ok else 404, {"ok": ok})
                if path == "/api/rules":
                    text = (data.get("text") or "").strip()
                    if not text:
                        return self._json(400, {"error": "text is required"})
                    rid = agent.memory.add_rule(text, data.get("category") or "convention",
                                                data.get("language") or "*", source="dashboard")
                    return self._json(200, {"added": rid is not None, "id": rid})
            return self._json(404, {"error": "not found"})

        # ----------------------------------------------------------- webhooks
        def _github_webhook(self, raw: bytes):
            secret = os.getenv("GITHUB_WEBHOOK_SECRET")
            if not secret:
                return self._json(503, {"error": "GITHUB_WEBHOOK_SECRET not configured"})
            expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, self.headers.get("X-Hub-Signature-256", "")):
                return self._json(401, {"error": "bad signature"})
            if self.headers.get("X-GitHub-Event") != "pull_request":
                return self._json(200, {"ignored": "not a pull_request event"})
            data = self._json_body(raw)
            if data is None:
                return
            if data.get("action") not in ("opened", "synchronize", "reopened"):
                return self._json(200, {"ignored": f"action {data.get('action')}"})
            repo, number = data["repository"]["full_name"], data["pull_request"]["number"]
            app.background(jobs.review_github_pr, repo, number, True, True)
            return self._json(202, {"accepted": f"{repo}#{number}"})

        def _gitlab_webhook(self, raw: bytes):
            token = os.getenv("GITLAB_WEBHOOK_TOKEN")
            if not token:
                return self._json(503, {"error": "GITLAB_WEBHOOK_TOKEN not configured"})
            if not hmac.compare_digest(self.headers.get("X-Gitlab-Token", ""), token):
                return self._json(401, {"error": "bad token"})
            data = self._json_body(raw)
            if data is None:
                return
            if data.get("object_kind") != "merge_request":
                return self._json(200, {"ignored": "not a merge_request event"})
            attrs = data.get("object_attributes", {})
            action = attrs.get("action")
            # "update" fires for title edits too; only review when new commits arrived.
            if action not in ("open", "reopen") and not (action == "update" and attrs.get("oldrev")):
                return self._json(200, {"ignored": f"action {action}"})
            project, iid = data["project"]["path_with_namespace"], attrs["iid"]
            app.background(jobs.review_gitlab_mr, project, iid, True, True)
            return self._json(202, {"accepted": f"{project}!{iid}"})

        # -------------------------------------------------------------- slack
        def _slack(self, path: str, raw: bytes):
            secret = os.getenv("SLACK_SIGNING_SECRET", "")
            if not secret:
                return self._json(503, {"error": "SLACK_SIGNING_SECRET not configured"})
            if not slack.verify_signature(secret, self.headers.get("X-Slack-Request-Timestamp", ""),
                                          raw, self.headers.get("X-Slack-Signature", "")):
                return self._json(401, {"error": "bad signature"})
            form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
            with app.session() as agent:
                if path == "/slack/commands":
                    text = slack.handle_command(agent, form.get("text", ""))
                    return self._json(200, {"response_type": "ephemeral", "text": text})
                try:
                    payload = json.loads(form.get("payload", "{}"))
                except ValueError:
                    return self._json(400, {"error": "bad payload"})
                text = slack.handle_interaction(agent, payload)
            if payload.get("response_url"):
                slack.respond(payload["response_url"], text)
            return self._json(200, {"text": text})

    return Handler


def create_server(app: ReviewerApp, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(app))


def serve(host: str = "127.0.0.1", port: int = 8080, demo: bool = False,
          verbose: bool = False, open_browser: bool = False) -> int:
    client = None
    cfg = Config()
    if demo:
        from .demo import DemoClient, seed_demo_data
        client = DemoClient()
        cfg.data_dir = cfg.data_dir / "demo"   # demo data never mixes with real data
    app = ReviewerApp(cfg, client=client, demo=demo, verbose=verbose)
    if demo:
        with app.session() as agent:           # every demo run starts from a clean, seeded state
            agent.memory.reset()
            seed_demo_data(agent.memory)
    try:
        srv = create_server(app, host, port)
    except OSError as e:
        print(f"\nCan't start on port {port}: {e.strerror or e}.\n"
              f"Another program (maybe an older Reviewer window) is using it.\n"
              f"Close it, or use another port:  reviewer serve"
              f"{' --demo' if demo else ''} --port {port + 1}", file=sys.stderr)
        return 1
    shown = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    url = f"http://{shown}:{port}"
    print(f"Reviewer dashboard: {url}   (Ctrl-C to stop)"
          + ("   [demo mode: no API key needed, data resets on every start]" if demo else ""))
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        srv.server_close()
    return 0
