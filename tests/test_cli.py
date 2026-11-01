import contextlib
import io
import unittest

from mr import cli, exitcodes
from mr.__main__ import main


class TestCliFlagMatrix(unittest.TestCase):
    def _run(self, argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(argv)
        return code, err.getvalue()

    def test_flag_matrix(self):
        type_vals = (
            "code",
            "bugfix",
            "refactor",
            "test",
            "ios-native",
            "ui",
            "review",
            "plan",
            "explain",
            "docs",
        )
        for v in type_vals:
            code, err = self._run(["route", "--type", v])
            self.assertEqual(code, exitcodes.USAGE, err)
            self.assertIn("task text missing", err)

        for flag, vals in (
            ("--difficulty", ("trivial", "easy", "medium", "hard", "critical")),
            ("--route", ("design", "judgment", "cascade")),
            ("--budget", ("free", "low", "normal", "high")),
            ("--latency", ("normal", "fast")),
            ("--judge", ("off", "shadow", "on")),
            ("--classifier", ("off", "shadow", "on")),
        ):
            for v in vals:
                code, err = self._run(["route", flag, v])
                self.assertEqual(code, exitcodes.USAGE, (flag, v, err))
                self.assertNotIn("invalid choice", err, err)
                if flag == "--judge":
                    self.assertNotIn("judge not built", err)

        prefer_ok = (
            "G-l",
            "G-m",
            "G-h",
            "G-x",
            "C25",
            "K-h",
            "K-x",
            "S",
            "S+",
            "O",
            "O+",
        )
        for v in prefer_ok:
            code, err = self._run(["run", "--prefer", v])
            self.assertEqual(code, exitcodes.USAGE)
            self.assertNotIn("invalid --prefer", err)

        for flag, bad in (
            ("--type", "bogus"),
            ("--difficulty", "extreme"),
            ("--route", "magic"),
            ("--budget", "huge"),
            ("--latency", "slow"),
            ("--judge", "maybe"),
            ("--classifier", "maybe"),
        ):
            code, err = self._run(["route", flag, bad])
            self.assertEqual(code, 2, err)
            self.assertIn(flag, err)

        for bad in ("nope", "cursor:gpt", "cursor:sonnet", "cursor:opus"):
            code, err = self._run(["route", "--prefer", bad])
            self.assertEqual(code, 2, err)
            self.assertIn("--prefer", err)

        for bad in ("-1", "x"):
            code, err = self._run(["run", "--wait-lock-s", bad])
            self.assertEqual(code, 2, err)
            self.assertIn("--wait-lock-s", err)

        for cmd in ("plan", "spec", "judge", "eval", "mark"):
            code, err = self._run([cmd, "extra", "args"])
            self.assertEqual(code, 2, err)
            self.assertIn(f"mr {cmd}: not built in v1 L0", err)

        code, _ = self._run(["--json", "route", "--type", "code"])
        self.assertEqual(code, exitcodes.USAGE)
        code, _ = self._run(["route", "--type", "code", "--json"])
        self.assertEqual(code, exitcodes.USAGE)

        self.assertEqual(self._run(["--help"])[0], 0)
        self.assertEqual(self._run(["route", "--help"])[0], 0)

        code, err = self._run(["route", "--bogus"])
        self.assertEqual(code, 2)
        self.assertIn("--bogus", err)


class TestExitPrecedence(unittest.TestCase):
    def test_precedence(self):
        self.assertEqual(exitcodes.precedence([0, 0]), 0)
        self.assertEqual(exitcodes.precedence([0, 1]), 1)
        self.assertEqual(exitcodes.precedence([3, 75]), 3)
        self.assertEqual(exitcodes.precedence([4, 1]), 4)


if __name__ == "__main__":
    unittest.main()
