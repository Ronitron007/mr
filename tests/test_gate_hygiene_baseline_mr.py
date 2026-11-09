"""gate.hygiene_baseline: H baseline on the pre-planner tree, per-case failure ids (issue 1, findings 1 and 7)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

from mr import config, hygiene
from mr.defaults import defaults
from tests.test_loop_run import GL, LoopCase

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


NODE_RED = "const t=require('node:test');const a=require('assert');t('known red',()=>a.equal(1,2));\n"
NODE_OLD = ("const t=require('node:test');const a=require('assert');const c=require('./calc');"
            "t('old green',()=>a.equal(c.old(),1));\n")
KNOWN_RED = "import unittest\n\n\nclass K(unittest.TestCase):\n    def test_k(self):\n        self.fail('known red')\n"
DISCOVER = sys.executable + " -m unittest discover -s tests"


@unittest.skipUnless(shutil.which("node"), "needs node")
class TestUnparsedRedBaseRefused(LoopCase):
    """Review finding h.unparsed_fallback_masks: a red base that mr cannot split into cases stops the run."""
    repo_files = {"a.test.js": NODE_RED, "b.test.js": NODE_OLD, "calc.js": "exports.old=()=>1;\n",
                  ".gitignore": "node_modules/\n"}
    hygiene_cmds = ["node --test"]

    def test_red_unparsed_base_exits_4_before_any_model_call(self):
        self.cursor({"files": {"calc.js": "exports.old=()=>9999;\nexports.neu=()=>2;\n"}}, model=GL)
        code, doc, err = self.run_json("--type", "code", "--difficulty", "trivial", text="add neu() to calc.js")
        self.assertEqual(code, 4, err)
        self.assertEqual(self.calls(), [], "no model call")
        lines = [ln for ln in err.splitlines() if "node --test" in ln]
        self.assertEqual(len(lines), 1, err)
        self.assertIn("already fails on the base tree", lines[0])
        task = self.only_task()
        self.assertEqual(task["refused"]["commands"], ["node --test"], task)
        self.assertIn("node --test", task["refused"]["reason"])
        self.assert_schema()


class TestParsedRedBaseRuns(LoopCase):
    repo_files = {"calc.py": "def old():\n    return 1\n", "tests/test_known.py": KNOWN_RED,
                  ".gitignore": "__pycache__/\n"}
    hygiene_cmds = [DISCOVER]

    def test_red_base_with_case_ids_still_runs(self):
        self.cursor({"files": {"calc.py": "def old():\n    return 1\ndef neu():\n    return 2\n"}}, model=GL)
        code, doc, err = self.run_json("--type", "code", "--difficulty", "trivial", text="add neu() to calc.py")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.models(), [GL])
        self.assertIs(self.state_of(doc["task"])["rungs"][-1]["signals"]["H"], True)
        self.assertNotIn("refused", self.only_task())


if __name__ == "__main__":
    unittest.main()
