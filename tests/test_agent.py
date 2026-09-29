import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from reviewer.agent import ReviewAgent, extract_json
from reviewer.config import Config
from reviewer.memory import Memory

DIFF = """diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -10,3 +10,4 @@ def connect():
     conn = make()
+    conn.execute("SELECT * FROM t WHERE id=" + uid)
     return conn
     # end
"""


class FakeClient:
    """Mimics anthropic.Anthropic().messages.create and records prompts."""

    def __init__(self, payload):
        self.payload = payload
        self.prompts = []
        self.messages = self

    def create(self, **kw):
        self.prompts.append(kw["messages"][0]["content"])
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


SQL_FINDING = {"file": "app/db.py", "line": 11, "severity": "blocker", "category": "security",
               "pattern_key": "sql-string-concat", "message": "SQL built by string concat",
               "suggestion": "use parameters"}


def make_agent(payload, tmp):
    cfg = Config(data_dir=Path(tmp))
    return ReviewAgent(cfg, Memory(":memory:"), FakeClient(payload))


class AgentTests(unittest.TestCase):
    def test_extract_json_fenced(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(extract_json('Sure! {"a": 2} done'), {"a": 2})

    def test_review_records_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = make_agent({"summary": "bad", "findings": [SQL_FINDING]}, tmp)
            res = a.review(DIFF)
            self.assertEqual(len(res.findings), 1)
            self.assertIn("id", res.findings[0])
            self.assertEqual(a.memory.stats()["findings"], 1)

    def test_hallucinated_file_and_bad_line_handled(self):
        with tempfile.TemporaryDirectory() as tmp:
            ghost = dict(SQL_FINDING, file="ghost.py")
            far = dict(SQL_FINDING, pattern_key="other", line=500)
            a = make_agent({"summary": "", "findings": [ghost, far]}, tmp)
            res = a.review(DIFF)
            self.assertEqual(len(res.findings), 1)
            self.assertIsNone(res.findings[0]["line"])

    def test_blocker_resurfaces_even_after_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = make_agent({"summary": "", "findings": [SQL_FINDING]}, tmp)   # severity: blocker
            first = a.review(DIFF)
            a.memory.set_status(first.findings[0]["id"], "rejected", reason="we use an ORM")
            second = a.review(DIFF)
            self.assertEqual(len(second.findings), 1)          # NOT hidden
            self.assertEqual(second.suppressed, [])
            self.assertTrue(second.findings[0]["previously_rejected"])

    def test_blocker_protection_is_configurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = make_agent({"summary": "", "findings": [SQL_FINDING]}, tmp)
            a.config.never_suppress_severities = ()
            first = a.review(DIFF)
            a.memory.set_status(first.findings[0]["id"], "rejected")
            self.assertEqual(a.review(DIFF).findings, [])

    def test_rejected_suggestion_never_repeats_and_reaches_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = make_agent({"summary": "", "findings": [dict(SQL_FINDING, severity="major")]}, tmp)
            first = a.review(DIFF)
            a.memory.set_status(first.findings[0]["id"], "rejected", reason="uses safe ORM wrapper")

            second = a.review(DIFF)
            self.assertEqual(second.findings, [])
            self.assertEqual(len(second.suppressed), 1)
            # the pushback + reason were injected into the second prompt
            self.assertIn("uses safe ORM wrapper", a.client.prompts[1])
            self.assertNotIn("uses safe ORM wrapper", a.client.prompts[0])

    def test_accepted_pattern_shows_up_as_common_mistake(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = make_agent({"summary": "", "findings": [SQL_FINDING]}, tmp)
            r = a.review(DIFF)
            a.memory.set_status(r.findings[0]["id"], "accepted")
            a.review(DIFF)
            self.assertIn("Mistakes this team makes often", a.client.prompts[1])

    def test_standards_file_and_rules_in_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "standards.md").write_text("All services must use structured logging.")
            a = make_agent({"summary": "", "findings": []}, tmp)
            a.memory.add_rule("Repositories never call HTTP clients", "architecture", "python")
            a.review(DIFF)
            p = a.client.prompts[0]
            self.assertIn("structured logging", p)
            self.assertIn("Repositories never call HTTP clients", p)

    def test_learn_from_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"rules": [{"text": "Use Result types, not exceptions, in domain code",
                                  "category": "architecture", "language": "*"}]}
            a = make_agent(payload, tmp)
            added = a.learn_from_comments(["please don't raise here, return Result"])
            self.assertEqual(len(added), 1)
            self.assertEqual(len(a.memory.list_rules()), 1)

    def test_plain_code_is_reviewed_as_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = make_agent({"summary": "ok", "findings": [
                {"file": "pasted_code.py", "line": 1, "severity": "minor", "category": "bug",
                 "pattern_key": "x", "message": "m", "suggestion": "s"}]}, tmp)
            res = a.review('print("hello")')
            self.assertEqual(len(res.findings), 1)
            self.assertIn("pasted_code.py", a.client.prompts[0])

    def test_garbage_input_gives_clear_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = make_agent({}, tmp)
            res = a.review("diff --git a/x b/x\nindex 1..2\n")   # diff header, no hunks
            self.assertIn("Couldn't find", res.summary)
            self.assertEqual(a.client.prompts, [])

    def test_bad_json_is_retried_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = make_agent("this is not json at all", tmp)
            good = json.dumps({"summary": "fine", "findings": []})
            replies = iter(["oops, no json here", good])
            a.client.payload = None
            a.client.create = lambda **kw: SimpleNamespace(
                content=[SimpleNamespace(type="text", text=next(replies))])
            self.assertEqual(a.review(DIFF).summary, "fine")

    def test_bad_json_twice_gives_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = make_agent("never json", tmp)
            with self.assertRaises(ValueError) as cm:
                a.review(DIFF)
            self.assertIn("unreadable", str(cm.exception))

    def test_missing_api_key_message(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=True):
            a = ReviewAgent(Config(data_dir=Path(tempfile.gettempdir())), Memory(":memory:"))
            with self.assertRaises(RuntimeError) as cm:
                a.client
            self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))

    def test_empty_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = make_agent({}, tmp)
            self.assertEqual(a.review("   ").summary, "No changes to review.")
            self.assertEqual(a.client.prompts, [])


if __name__ == "__main__":
    unittest.main()
