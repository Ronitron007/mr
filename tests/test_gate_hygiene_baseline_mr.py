"""gate.hygiene_baseline: H baseline on the pre-planner tree, per-case failure ids (issue 1, findings 1 and 7)."""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from mr import config, hygiene, patches
from mr.defaults import defaults
from tests.test_aj10_defer_mr import ALLOW, DENY, MEDIUM, STRAY, verdict
from tests.test_aj10_resume_mr import ResumeCase
from tests.test_loop_run import GH, GL, GM, GX, SONNET, TEST_PATH, LoopCase, mul
from tests.test_loop_run import PLAN as MUL_PLAN
from tests.test_loop_run import REVIEW_PASS as MUL_REVIEW_PASS
from tests.test_run_once import SRC

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
        base = hygiene.base_failures(self.tree, cfg, "demo")
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


class TestResumeOldBase(ResumeCase):
    """Review finding h.resume_old_base_ids: mr resume never uses the saved base ids, it measures them again."""
    repo_files = {**LoopCase.repo_files, "tests/test_known.py": KNOWN_RED}
    hygiene_cmds = [DISCOVER]

    def defer_then_age(self, **state_fields):
        code, doc, text = self.defer_scope(claude=[verdict("reject", "Plan again."), {"result": REVIEW_PASS}])
        self.assertEqual(code, 75, text)
        path = os.path.join(self.home, "tasks", doc["task"], "state.json")
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        st["pending"]["ctx"]["base_set"] = [f"cmd:{DISCOVER}:exit1"]  # what 8ead088 saved for this repo
        st.update(state_fields)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        self.guard(ALLOW)
        return doc["task"]

    def test_resume_of_an_old_state_does_not_flag_the_known_failure(self):
        tid = self.defer_then_age()
        code, doc, text = self.resume(tid)
        self.assertEqual(code, 0, text)
        last = self.state_of(tid)["rungs"][-1]
        self.assertEqual(last["rung"], "G-m", last)
        self.assertIs(last["signals"]["H"], True, last)
        self.assertIn(GM, self.models())

    def test_resume_off_its_reset_tree_exits_6(self):
        tid = self.defer_then_age(reset_point="4b825dc642cb6eb9a060e54bf8d69288fbee4904")
        before = len(self.calls())
        code, doc, text = self.resume(tid)
        self.assertEqual(code, 6, text)
        self.assertIn("cannot measure the hygiene base again", text)
        self.assertEqual(len(self.calls()), before, "no model call")


class TestFlakyBase(LoopCase):
    """Verify finding h.flaky_base_cached_refusal: one bad base run refuses that run only."""

    def flaky(self, body):
        flag = os.path.join(tempfile.mkdtemp(prefix="mr-flaky-"), "ran")
        self.addCleanup(shutil.rmtree, os.path.dirname(flag), True)
        self.hygiene_cmds = [f"sh -c 'if [ -f {flag} ]; then exit 0; fi; touch {flag}; {body}'"]
        self.write_cfg()

    def run_twice(self):
        codes = []
        for _ in range(2):
            self.cursor({"files": {"README.md": "# test repo\nfixed typo\n"}}, model=GL)
            code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial")
            codes.append((code, err))
        return codes

    def test_timeout_once_then_pass_is_not_refused_again(self):
        self.flaky("sleep 20")
        (first, first_err), (second, second_err) = self.run_twice()
        self.assertEqual(first, 4, first_err)
        self.assertIn("timed out on the base tree", first_err)
        self.assertEqual(second, 0, second_err)
        self.assertEqual(self.models(), [GL])

    def test_crash_once_then_pass_is_not_refused_again(self):
        self.flaky("exit 137")
        (first, first_err), (second, second_err) = self.run_twice()
        self.assertEqual(first, 4, first_err)
        self.assertIn("cannot tell its failures apart", first_err)
        self.assertEqual(second, 0, second_err)
        self.assertEqual(self.models(), [GL])


class TestSwitchReporter(LoopCase):
    """Verify finding h.cache_key_ignores_commands: a new hygiene command list gets its own base."""
    repo_files = {**LoopCase.repo_files, "tests/test_known.py": KNOWN_RED}
    hygiene_cmds = [DISCOVER + " >/dev/null 2>&1"]

    def test_parsable_reporter_after_refusal_masks_the_known_red_test(self):
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 4, err)
        self.hygiene_cmds = [DISCOVER]
        self.write_cfg()
        self.cursor({"files": {"README.md": "# test repo\nfixed typo\n"}}, model=GL)
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.state_of(doc["task"])["outcome"], "accepted")
        self.assertIs(self.state_of(doc["task"])["rungs"][-1]["signals"]["H"], True)


README_FIX = {"README.md": "# test repo\nfixed typo\n"}
DEP_LINT = "sh -c 'cat node_modules/lint.txt 2>/dev/null; test ! -s node_modules/lint.txt'"


class TwoRuns(LoopCase):
    def one(self):
        self.cursor({"files": README_FIX}, model=GL)
        return self.run_json("--type", "docs", "--difficulty", "trivial")

    def discard(self):
        subprocess.run(["git", "-C", self.repo, "reset", "-q", "--hard"], check=True)
        subprocess.run(["git", "-C", self.repo, "clean", "-qfd"], check=True)

    def dep_lint(self, text):
        os.makedirs(os.path.join(self.repo, "node_modules"), exist_ok=True)
        with open(os.path.join(self.repo, "node_modules", "lint.txt"), "w", encoding="utf-8") as fh:
            fh.write(text)


class TestTimeoutLowered(TwoRuns):
    """Verify finding h.cache_key_ignores_timeout: a lowered hygiene_s on a known tree refuses, it burns no rung."""
    hygiene_cmds = ["sh -c 'sleep 2; exit 0'"]

    def test_lowered_timeout_on_a_green_tree_exits_4_with_no_model_call(self):
        code, doc, err = self.one()
        self.assertEqual(code, 0, err)
        self.discard()
        with open(self.cfgpath, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["timeouts"] = {"hygiene_s": 100}  # x0.01 test time scale: 1 s, under the 2 s command
        with open(self.cfgpath, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)
        before = len(self.calls())
        code, doc, err = self.one()
        self.assertEqual(code, 4, err)
        self.assertIn("timed out on the base tree", err)
        self.assertEqual(len(self.calls()), before, "no model call")


class TestBaseMeasuredEachRun(TwoRuns):
    """The tree sha cannot see untracked deps: each run measures its base again."""
    hygiene_cmds = [DEP_LINT]

    def test_untracked_change_between_runs_is_in_the_new_base(self):
        code, doc, err = self.one()
        self.assertEqual(code, 0, err)
        self.discard()
        self.dep_lint("src/dep.py:1:1: E1 broken dep\n")
        code, doc, err = self.one()
        self.assertEqual(code, 0, err)
        self.assertIs(self.state_of(doc["task"])["rungs"][-1]["signals"]["H"], True)
        self.assertEqual(self.models(), [GL, GL])


class TestResumeMeasuresBase(ResumeCase):
    """mr resume measures the base again on the reset tree; it does not trust the saved base."""
    hygiene_cmds = [DEP_LINT]

    def test_untracked_change_while_deferred_is_in_the_resumed_base(self):
        code, doc, text = self.defer_scope(claude=[verdict("reject", "Plan again."), {"result": REVIEW_PASS}])
        self.assertEqual(code, 75, text)
        os.makedirs(os.path.join(self.repo, "node_modules"), exist_ok=True)
        with open(os.path.join(self.repo, "node_modules", "lint.txt"), "w", encoding="utf-8") as fh:
            fh.write("src/dep.py:1:1: E1 broken dep\n")
        self.guard(ALLOW)
        code, doc2, text = self.resume(doc["task"])
        self.assertEqual(code, 0, text)
        last = self.state_of(doc["task"])["rungs"][-1]
        self.assertEqual(last["rung"], "G-m", last)
        self.assertIs(last["signals"]["H"], True, last)


# The planner's red test stubs calc.f and fails before it puts it back, so test_z is red on the post-plan tree only.
LEAKY_RED = ("import calc\nimport unittest\n\n\nclass TestMul(unittest.TestCase):\n"
             "    def test_mul_basic(self):\n        old = calc.f\n        calc.f = lambda: 9\n"
             "        import mul\n        self.assertEqual(mul.mul(2, 3), 6)\n        calc.f = old\n")
VICTIM = ("import calc, unittest\n\n\nclass Z(unittest.TestCase):\n"
          "    def test_z(self):\n        self.assertEqual(calc.f(), 1)\n")
LEAK_PLAN = copy.deepcopy(MUL_PLAN)
LEAK_PLAN["tests"][0]["content"] = LEAKY_RED
LEAK_PLAN["scope"]["modify"] = ["README.md", "calc.py"]
BREAKS_CALC = {"files": {"mul.py": "def mul(a, b):\n    return a * b\n", "calc.py": "def f():\n    return 9\n"}}


class TestResumeBaseWithoutPlannerTests(ResumeCase):
    """Verify finding h.postplan_base_leak: the resumed base is the pre-plan tree, so a real regression fails H."""
    repo_files = {**LoopCase.repo_files, "tests/test_zcalc.py": VICTIM, "calc.py": "def f():\n    return 1\n"}
    hygiene_cmds = [DISCOVER]

    def test_regression_after_a_scope_defer_is_not_accepted(self):
        self.set_scenario({"cursor": {GH: [{"result": json.dumps(LEAK_PLAN)}],
                                      GX: [{"files": {**mul("*")["files"], STRAY: "x\n"}}]},
                           "claude": {SONNET: [verdict("reject", "the stray file is not wanted"), BREAKS_CALC,
                                               {"result": MUL_REVIEW_PASS}, {"result": MUL_REVIEW_PASS}]}},
                          check=DENY)
        code, doc, text = self.go(*MEDIUM)
        self.assertEqual(code, 75, text)
        tid = doc["task"]
        self.guard(ALLOW)
        code, _, text = self.resume(tid)
        st = self.state_of(tid)
        self.assertNotEqual(code, 0, text)
        self.assertNotEqual(st["outcome"], "accepted", st["rungs"])
        broke = [r for r in st["rungs"] if r["rung"] == "S"]
        self.assertTrue(broke, st["rungs"])
        self.assertIs(broke[0]["signals"]["H"], False, broke)
        self.assertTrue(self.exists(TEST_PATH), "the locked test is back on the tree")


class TestResumeBaseInterrupted(ResumeCase):
    """A signal or a crash while the planner's tests are aside puts them back or leaves the task refused."""

    def setUp(self):
        super().setUp()
        self.flag = os.path.join(self.dir, "kill-on-base")

    def defer(self, action):
        self.hygiene_cmds = [f"if [ -f {self.flag} ] && [ ! -f {TEST_PATH} ]; then {action}; fi; " + DISCOVER]
        self.write_cfg()
        self.set_scenario({"cursor": {GH: [{"result": json.dumps(MUL_PLAN)}],
                                      GX: [{"files": {**mul("*")["files"], STRAY: "x\n"}}]},
                           "claude": {SONNET: [verdict("accept", "harmless"), {"result": MUL_REVIEW_PASS}]}},
                          check=DENY)
        code, doc, text = self.go(*MEDIUM)
        self.assertEqual(code, 75, text)
        self.guard(ALLOW)
        with open(self.flag, "w", encoding="utf-8"):
            pass
        return doc["task"], self.state_of(doc["task"])

    def resume_process(self, tid):
        proc = subprocess.run([sys.executable, "-m", "mr", "--config", self.cfgpath, "--json", "resume", tid],
                              cwd=SRC, env=self.env, capture_output=True, text=True, timeout=120)
        return proc.returncode, proc.stdout + proc.stderr

    def locked_sha(self, tid):
        with open(os.path.join(self.home, "tasks", tid, "tests.lock"), encoding="utf-8") as fh:
            return json.load(fh)["files"][TEST_PATH]["sha256"]

    def test_sigterm_during_the_resumed_base_puts_the_locked_tests_back(self):
        tid, st = self.defer("kill -TERM $PPID; sleep 2")
        before = len(self.calls())
        code, text = self.resume_process(tid)
        self.assertEqual(code, 143, text)
        self.assertEqual(len(self.calls()), before, "no model call")
        with open(os.path.join(self.repo, TEST_PATH), "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), self.locked_sha(tid))
        self.assertEqual(patches.take_reset_tree(self.repo), st["reset_point"], "the tree is at its reset tree")

    def test_kill_during_the_resumed_base_leaves_the_task_refused(self):
        tid, st = self.defer("kill -KILL $PPID; sleep 2")
        before = len(self.calls())
        code, text = self.resume_process(tid)
        self.assertEqual(code, -9, text)
        self.assertFalse(self.exists(TEST_PATH))
        aside = os.path.join(self.home, "tasks", tid, "base-aside", TEST_PATH)
        with open(aside, "rb") as fh:
            self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), self.locked_sha(tid), "kept, not lost")
        for path in os.listdir(os.path.join(self.home, "state")):
            if path.endswith(".lock"):
                open(os.path.join(self.home, "state", path), "w").close()  # what the stale-lock line asks for
        code, _, text = self.resume(tid)
        self.assertEqual(code, 6, text)
        self.assertIn("base-aside", text)
        self.assertEqual(len(self.calls()), before, "no model call")

    def test_a_changed_locked_test_after_the_resumed_base_exits_4(self):
        tid, st = self.defer(f"for f in {self.home}/tasks/*/base-aside/{TEST_PATH}; do echo '# edited' >> $f; done")
        before = len(self.calls())
        code, _, text = self.resume(tid)
        self.assertEqual(code, 4, text)
        self.assertIn("the locked tests changed while mr measured the hygiene base: " + TEST_PATH, text)
        self.assertEqual(len(self.calls()), before, "no model call")


if __name__ == "__main__":
    unittest.main()
