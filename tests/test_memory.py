import unittest

from reviewer.memory import Memory


def finding(key="missing-error-handling", file="a.py", msg="No try/except around IO"):
    return {"pattern_key": key, "file": file, "line": 3, "message": msg,
            "category": "error-handling", "severity": "major", "suggestion": "wrap it"}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.m = Memory(":memory:")

    def test_rule_dedup_reinforces(self):
        self.assertIsNotNone(self.m.add_rule("Use dataclasses"))
        self.assertIsNone(self.m.add_rule("Use dataclasses"))
        self.assertEqual(self.m.list_rules()[0]["weight"], 1.5)

    def test_language_filter(self):
        self.m.add_rule("Use type hints", language="python")
        self.m.add_rule("Prefer const", language="javascript")
        self.m.add_rule("Write tests")
        texts = {r["text"] for r in self.m.list_rules(languages={"python"})}
        self.assertEqual(texts, {"Use type hints", "Write tests"})

    def test_rejected_finding_is_suppressed_same_file(self):
        f = self.m.record_review("r1", [finding()])[0]
        self.assertFalse(self.m.is_suppressed(f["pattern_key"], "a.py", f["message"]))
        self.m.set_status(f["id"], "rejected")
        self.assertTrue(self.m.is_suppressed(f["pattern_key"], "a.py", "Totally different words"))
        # other file, first rejection only -> still allowed
        self.assertFalse(self.m.is_suppressed(f["pattern_key"], "b.py", "x"))

    def test_teamwide_suppression_after_threshold(self):
        for i in range(2):
            f = self.m.record_review(f"r{i}", [finding(file=f"f{i}.py")])[0]
            self.m.set_status(f["id"], "rejected")
        self.assertTrue(self.m.is_suppressed("missing-error-handling", "new.py", "x", 2))

    def test_acceptance_keeps_pattern_alive_and_marks_frequent(self):
        for i in range(3):
            f = self.m.record_review(f"r{i}", [finding(file=f"f{i}.py")])[0]
            self.m.set_status(f["id"], "accepted")
        f = self.m.record_review("r9", [finding(file="z.py")])[0]
        self.m.set_status(f["id"], "rejected")
        self.assertFalse(self.m.is_suppressed("missing-error-handling", "new.py", "x", 2))
        self.assertEqual(self.m.frequent_mistakes()[0]["accepted"], 3)

    def test_reject_with_reason_creates_rule(self):
        f = self.m.record_review("r1", [finding()])[0]
        self.m.set_status(f["id"], "rejected", reason="we log at the caller")
        rules = self.m.list_rules()
        self.assertEqual(len(rules), 1)
        self.assertIn("we log at the caller", rules[0]["text"])

    def test_changing_verdict_does_not_double_count(self):
        f = self.m.record_review("r1", [finding()])[0]
        self.m.set_status(f["id"], "accepted")
        self.m.set_status(f["id"], "accepted")
        self.m.set_status(f["id"], "rejected")
        p = self.m.conn.execute("SELECT accepted, rejected FROM patterns").fetchone()
        self.assertEqual((p["accepted"], p["rejected"]), (0, 1))

    def test_reset_wipes_everything(self):
        self.m.add_rule("x")
        f = self.m.record_review("r1", [finding()])[0]
        self.m.set_status(f["id"], "accepted")
        self.m.reset()
        s = self.m.stats()
        self.assertEqual((s["rules"], s["findings"], s["patterns"]), (0, 0, 0))
        self.assertEqual(self.m.record_review("r2", [finding()])[0]["id"], 1)  # ids restart

    def test_stats(self):
        f = self.m.record_review("r1", [finding()])[0]
        self.m.set_status(f["id"], "accepted")
        s = self.m.stats()
        self.assertEqual(s["acceptance_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
