"""Offline demo: a scripted 'model' so the whole learning loop can be shown without an API key.

`reviewer demo`          narrated walkthrough in the terminal
`reviewer serve --demo`  dashboard whose "Run review" button uses this fake model
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from .agent import ReviewAgent
from .config import Config
from .diff_utils import new_file_diff
from .memory import Memory

SAMPLE_FILE = '''import logging
import requests

log = logging.getLogger(__name__)


class UserRepository:
    def __init__(self, conn):
        self.conn = conn

    def find_by_email(self, email):
        query = "SELECT * FROM users WHERE email = '" + email + "'"
        return self.conn.execute(query).fetchall()

    def login(self, email, password):
        log.info("login attempt %s / %s", email, password)
        try:
            user = self.find_by_email(email)
        except:
            return None
        return user

    def enrich(self, email):
        return requests.get("https://api.example.com/u/" + email).json()
'''

SAMPLE_PATH = "app/repos/users.py"


SAMPLE_DIFF = new_file_diff(SAMPLE_PATH, SAMPLE_FILE)


def _line(snippet: str) -> int:
    for i, line in enumerate(SAMPLE_FILE.splitlines(), 1):
        if snippet in line:
            return i
    raise ValueError(snippet)


def _review_payload() -> dict:
    f = SAMPLE_PATH
    return {
        "summary": "New user repository with a SQL injection, a credential leak in logs and "
                   "some architecture/error-handling issues.",
        "findings": [
            {"file": f, "line": _line("SELECT * FROM users"), "severity": "blocker",
             "category": "security", "pattern_key": "sql-string-concat",
             "message": "SQL is built by concatenating user input, which allows SQL injection.",
             "suggestion": "Use a parameterized query: conn.execute('... WHERE email = ?', (email,))"},
            {"file": f, "line": _line("log.info"), "severity": "blocker",
             "category": "security", "pattern_key": "secret-in-logs",
             "message": "The plaintext password is written to the logs.",
             "suggestion": "Log only the email (or a user id). Never log credentials."},
            {"file": f, "line": _line("except:"), "severity": "major",
             "category": "error-handling", "pattern_key": "bare-except",
             "message": "Bare `except:` swallows every error including KeyboardInterrupt and hides failures.",
             "suggestion": "Catch the specific database error and log it before returning."},
            {"file": f, "line": _line("requests.get"), "severity": "major",
             "category": "architecture", "pattern_key": "repository-calls-http",
             "message": "A repository is making an HTTP call, which breaks the handlers -> services "
                        "-> repositories layering.",
             "suggestion": "Move the enrichment call into a service and inject the result."},
            {"file": f, "line": _line("def find_by_email"), "severity": "nit",
             "category": "convention", "pattern_key": "missing-type-hints",
             "message": "Public method has no type hints.",
             "suggestion": "def find_by_email(self, email: str) -> list[tuple]:"},
        ],
    }


LEARN_PAYLOAD = {"rules": [
    {"text": "Never log credentials or tokens, even at debug level", "category": "security",
     "language": "*"},
    {"text": "Database access goes through parameterized queries only", "category": "security",
     "language": "*"},
]}


class DemoClient:
    """Drop-in for anthropic.Anthropic(): returns canned but realistic JSON."""

    def __init__(self):
        self.messages = self
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        system = kwargs.get("system", "")
        content = kwargs["messages"][0]["content"] if kwargs.get("messages") else ""
        if "extract durable" in system:
            payload = LEARN_PAYLOAD
        elif f"=== FILE: {SAMPLE_PATH} ===" in content:   # only the diff itself, not team memory
            payload = _review_payload()
        else:
            payload = {"summary": "Demo mode: the offline model only knows the built-in sample diff, "
                                  "so it can't review other code. Click 'Load a sample diff', or run "
                                  "without --demo (with ANTHROPIC_API_KEY) to review real code.",
                       "findings": []}
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))])


def seed_demo_data(memory: Memory) -> bool:
    """Give the dashboard some history so it isn't empty on first launch."""
    s = memory.stats()
    if s["findings"] or s["rules"]:
        return False
    memory.add_rule("Layering: handlers -> services -> repositories, never skip a layer",
                    "architecture", "*", source="manual")
    memory.add_rule("Type hints on all public functions", "convention", "python", source="manual")
    memory.add_rule("Never log credentials or tokens, even at debug level", "security", "*",
                    source="history")

    def past(review_id, key, file, msg, cat, sev, verdicts):
        for status, reason in verdicts:
            f = memory.record_review(review_id, [{
                "pattern_key": key, "file": file, "line": 10, "message": msg,
                "category": cat, "severity": sev, "suggestion": "see message"}])[0]
            memory.set_status(f["id"], status, reason)

    past("seed0001", "sql-string-concat", "app/repos/orders.py",
         "SQL built with string concatenation.", "security", "blocker",
         [("accepted", None)] * 4)
    past("seed0002", "missing-error-handling", "app/services/billing.py",
         "Network call without error handling.", "error-handling", "major",
         [("accepted", None)] * 3 + [("rejected", "retries are handled by the HTTP client wrapper")])
    past("seed0003", "missing-docstring", "app/services/misc.py",
         "Public function has no docstring.", "readability", "nit",
         [("rejected", "we document in the wiki, not in code")] * 2)
    return True


def run_demo(out=print) -> None:
    """Narrated walkthrough of learn -> review -> feedback -> smarter review."""
    from .cli import print_result  # local import: cli imports this module

    if out is print and not sys.stdout.isatty():  # no colour codes when piped / redirected
        out = lambda text: print(re.sub(r"\033\[[0-9;]*m", "", text))  # noqa: E731

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(data_dir=Path(tmp))
        agent = ReviewAgent(cfg, Memory(cfg.db_path), DemoClient())
        try:
            out("\n\033[1m1. Teach the agent your team's standards\033[0m")
            agent.memory.add_rule("Repositories never call HTTP clients", "architecture", "python")
            out("   + rule added by hand: 'Repositories never call HTTP clients'")
            out("   ...and learning from past review comments:")
            for r in agent.learn_from_comments(["please don't log the password!",
                                                "use bind params, never string concat in SQL"]):
                out(f"   + learned: {r['text']}")

            out("\n\033[1m2. First review of a new file\033[0m")
            first = agent.review(SAMPLE_DIFF, "Add UserRepository")
            print_result(first)

            out("\n\033[1m3. The team gives feedback\033[0m")
            by_key = {f["pattern_key"]: f for f in first.findings}
            for key in ("sql-string-concat", "secret-in-logs", "bare-except"):
                agent.memory.set_status(by_key[key]["id"], "accepted")
                out(f"   ✔ accepted  #{by_key[key]['id']} {key}")
            agent.memory.set_status(by_key["missing-type-hints"]["id"], "rejected",
                                    "legacy module, we add typing incrementally")
            out(f"   ✘ rejected  #{by_key['missing-type-hints']['id']} missing-type-hints "
                "(reason saved as a team rule)")
            agent.memory.set_status(by_key["repository-calls-http"]["id"], "rejected",
                                    "this repository is an adapter for the gateway, HTTP is allowed")
            out(f"   ✘ rejected  #{by_key['repository-calls-http']['id']} repository-calls-http")

            out("\n\033[1m4. What the agent now injects into every review prompt\033[0m")
            for line in agent._memory_context([SAMPLE_PATH]).splitlines():
                out("   | " + line)

            out("\n\033[1m5. Same change reviewed again: no repeated suggestions\033[0m")
            second = agent.review(SAMPLE_DIFF, "Add UserRepository")
            second_ids = [f["id"] for f in second.findings]
            for f in second.findings:  # give the fresh findings a verdict so stats look real
                agent.memory.set_status(f["id"], "accepted")
            out(f"   first review : {len(first.findings)} findings")
            out(f"   second review: {len(second_ids)} findings, "
                f"{len(second.suppressed)} hidden (already rejected by the team)")
            out("   hidden: " + ", ".join(f['pattern_key'] for f in second.suppressed))

            s = agent.memory.stats()
            out(f"\n\033[1mMemory now holds\033[0m {s['rules']} rules, {s['findings']} findings, "
                f"{s['patterns']} patterns.\n")
        finally:
            agent.memory.close()
