import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from reviewer import cli
from reviewer.demo import SAMPLE_DIFF


class CliTests(unittest.TestCase):
    def run_cli(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                cli.main(argv)
        return cm.exception.code, out.getvalue(), err.getvalue()

    def test_missing_key_is_a_readable_error_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.diff")
            with open(path, "w") as fh:
                fh.write(SAMPLE_DIFF)
            code, _, err = self.run_cli(["review", "--diff-file", path], {"REVIEWER_HOME": tmp})
        self.assertEqual(code, 1)
        self.assertIn("ANTHROPIC_API_KEY", err)
        self.assertNotIn("Traceback", err)

    def test_demo_runs_and_has_no_colour_codes_when_piped(self):
        code, out, _ = self.run_cli(["demo"], {})
        self.assertEqual(code, 0)
        self.assertIn("no repeated suggestions", out)
        self.assertNotIn("\033[", out)

    def test_teach_rules_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"REVIEWER_HOME": tmp}
            self.assertEqual(self.run_cli(["teach", "Use pathlib"], env)[0], 0)
            code, out, _ = self.run_cli(["rules"], env)
            self.assertIn("Use pathlib", out)
            self.assertIn("Rules learned: 1", self.run_cli(["stats"], env)[1])


if __name__ == "__main__":
    unittest.main()
