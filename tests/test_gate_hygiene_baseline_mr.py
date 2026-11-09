"""gate.hygiene_baseline: H baseline on the pre-planner tree, per-case failure ids (issue 1, findings 1 and 7)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

from mr import config, hygiene
from mr.defaults import defaults
from tests.test_loop_run import LoopCase

OLD_TEST = ("import unittest\nimport calc\n\n\nclass Old(unittest.TestCase):\n"
            "    def test_old(self):\n        self.assertEqual(calc.old(), 1)\n")
NEW_TEST = ("import unittest\nimport calc\n\n\nclass New(unittest.TestCase):\n"
            "    def test_new(self):\n        self.assertEqual(calc.new(), 2)\n")
NEW_PATH = "tests/test_new_mr.py"
PLAN = {
    "goal": "Add new()",
    "scope": {"modify": ["calc.py"], "create": [NEW_PATH], "forbidden": []},
    "requirements": [{"id": "R1", "text": "new returns 2", "examples": [{"given": "", "when": "new()", "then": "2"}]}],
    "acceptance": [{"id": "A1", "covers": ["R1"], "file": NEW_PATH, "tests": ["test_new"]}],
    "est_loc": 10, "files_map": {"R1": ["calc.py"]}, "tests": [{"path": NEW_PATH, "content": NEW_TEST}],
}
REVIEW_PASS = 'MR_REVIEW {"items":[{"id":"R1","verdict":"pass","evidence":"calc.py:4"}],"overall":"pass"}'
REGRESSED = {"files": {"calc.py": "def old():\n    return 9999\ndef new():\n    return 2\n"}}


class TestRegressionNotHidden(LoopCase):
    """Codex reproduction regression-hidden-by-red-baseline: red planner tests must not mask a broken old test."""
    repo_files = {"calc.py": "def old():\n    return 1\n", "tests/__init__.py": "", "tests/test_old.py": OLD_TEST,
                  ".gitignore": "__pycache__/\n"}
    hygiene_cmds = [sys.executable + " -m unittest discover -s tests"]

    def test_broken_old_test_is_not_accepted(self):
        self.set_scenario({"cursor": {"grok-4.7-high": [{"result": json.dumps(PLAN)}, REGRESSED]},
                           "claude": {"claude-sonnet-5-5": [{"result": REVIEW_PASS}]}})
        code, doc, err = self.run_json("--type", "code", "--difficulty", "medium", text="add new() to calc.py")
        self.assertNotEqual(code, 0, err)
        first = self.state_of(doc["task"])["rungs"][0]
        self.assertIs(first["signals"]["H"], False, first)
        self.assertIs(first["signals"]["A"], True, first)  # the change did make the locked test pass
        handoff = next(c["prompt"] for c in self.calls()[2:] if "New hygiene failures:" in c["prompt"])
        h_line = next(ln for ln in handoff.splitlines() if ln.startswith("New hygiene failures:"))
        self.assertIn("test_old", h_line)
        self.assertNotIn("test_new", h_line)  # locked tests are signal A's job


def _cfg(home, commands):
    return config.from_dict(config._deep_merge(defaults(), {
        "paths": {"home": home}, "hygiene": {"commands": commands}, "timeouts": {"hygiene_s": 30}}))


class TestFailureIds(unittest.TestCase):
    def setUp(self):
        self.tree = tempfile.mkdtemp(prefix="mr-hygb-")
        self.addCleanup(shutil.rmtree, self.tree, True)

    def write(self, rel, text):
        path = os.path.join(self.tree, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_missing_tsc_command_fails(self):
        # Codex reproduction missing-tsc-command-passes-hygiene
        got = hygiene.run_checks(self.tree, ["tsc-audit-command-that-does-not-exist"], 10)
        self.assertTrue(got, "a tsc command that exits nonzero with no diagnostic is a failure")

    def test_vitest_failed_suite_fails(self):
        # Codex reproduction vitest-suite-failure-dropped
        report = {"testResults": [{"name": "a.test.ts", "assertionResults": [], "status": "failed",
                                   "message": "Failed to load setup file"}]}
        got = hygiene._parse(self.tree, "npx vitest --reporter=json", 1, json.dumps(report), "")
        self.assertTrue(got, "a failed vitest suite with no assertion results is a failure")
        self.assertTrue(all("a.test.ts" in fid for fid in got), got)

    def test_red_base_masks_only_the_same_case(self):
        self.write("tests/__init__.py", "")
        self.write("tests/test_a.py", "import unittest\n\n\nclass A(unittest.TestCase):\n"
                   "    def test_known(self):\n        self.fail('old')\n")
        cfg = _cfg(os.path.join(self.tree, ".home"), {"demo": [sys.executable + " -m unittest discover -s tests"]})
        base = hygiene.base_failures(self.tree, cfg, "demo", "pre-plan")
        self.assertEqual(len(base), 1, base)
        self.assertTrue(hygiene.hygiene(self.tree, cfg, base, "demo").ok)
        self.write("tests/test_b.py", "import unittest\n\n\nclass B(unittest.TestCase):\n"
                   "    def test_broken(self):\n        self.fail('new')\n")
        sig = hygiene.hygiene(self.tree, cfg, base, "demo")
        self.assertFalse(sig.ok)
        self.assertEqual(len(sig.new_failures), 1, sig.new_failures)
        self.assertIn("test_broken", sig.new_failures[0])
        locked = hygiene.hygiene(self.tree, cfg, base, "demo", locked_ids=frozenset({"tests/test_b.py"}))
        self.assertTrue(locked.ok, locked.detail)

    def test_pytest_summary_ids(self):
        summary = ("FAILED tests/test_a.py::A::test_known - AssertionError: old\n"
                   "ERROR tests/test_c.py - ImportError: nope\n"
                   "FAILED (failures=1)\n")
        got = hygiene._parse(self.tree, "pytest -q", 1, summary, "")
        self.assertEqual(got, {"pytest:tests/test_a.py:FAILED ::A::test_known", "pytest:tests/test_c.py:ERROR "})

    def test_repeated_diagnostic_is_new(self):
        line = "src/a.ts(3,1): error TS2322: Type 'string' is not assignable to type 'number'.\n"
        base = hygiene._parse(self.tree, "npx tsc --noEmit", 2, line, "")
        twice = hygiene._parse(self.tree, "npx tsc --noEmit", 2, line + line.replace("(3,", "(9,"), "")
        self.assertEqual(len(twice - base), 1, (base, twice))


if __name__ == "__main__":
    unittest.main()
