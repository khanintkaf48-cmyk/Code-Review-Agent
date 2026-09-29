import unittest

from reviewer.diff_utils import parse_diff, snap_line, truncate

DIFF = """diff --git a/app/db.py b/app/db.py
index 111..222 100644
--- a/app/db.py
+++ b/app/db.py
@@ -10,3 +10,5 @@ def connect():
     conn = make()
-    return conn
+    conn.execute("SELECT * FROM t WHERE id=" + uid)
+    return conn
     # end
"""


class DiffTests(unittest.TestCase):
    def test_parse_lines(self):
        p = parse_diff(DIFF)
        self.assertEqual(p.files, ["app/db.py"])
        self.assertEqual(p.added_lines["app/db.py"], {11, 12})
        self.assertIn("   11 +", p.annotated)
        self.assertIn("=== FILE: app/db.py ===", p.annotated)

    def test_snap(self):
        p = parse_diff(DIFF)
        self.assertEqual(snap_line(p, "app/db.py", 12), 12)
        self.assertEqual(snap_line(p, "app/db.py", 14), 12)
        self.assertIsNone(snap_line(p, "app/db.py", 90))
        self.assertIsNone(snap_line(p, "nope.py", 1))

    def test_truncate(self):
        text, cut = truncate("a\n" * 100, 20)
        self.assertTrue(cut)
        self.assertIn("truncated", text)


if __name__ == "__main__":
    unittest.main()
