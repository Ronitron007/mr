"""gate.no_checks: `mr run` refuses a repo with no hygiene commands before any model call; --no-checks runs anyway."""
import json
import os
import shutil
import tempfile
import unittest

from mr import exitcodes
from mr.config import load
from tests.test_aj10_defer_mr import ALLOW, DENY, EASY, STRAYED, WHY_ACCEPT, verdict
from tests.test_aj10_resume_mr import ResumeCase
from tests.test_loop_run import GL, GM, README, REVIEW_PASS, SONNET, LoopCase
from tests.test_run_once import RunCase
from tests import test_setup_mr

TRIVIAL = ("--type", "code", "--difficulty", "trivial")


def _refusal_lines(case, err):
    lines = [ln for ln in err.splitlines() if "hygiene.commands.repo" in ln]
    case.assertEqual(len(lines), 1, err)
    case.assertIn("--no-checks", lines[0])
    return lines[0]


class NoHygiene(LoopCase):
    hygiene_cmds = []


class TestRefused(NoHygiene):
    def test_trivial_code_exits_4_before_any_model_call(self):
        self.cursor({"files": README}, model=GL)
        code, doc, err = self.run_json(*TRIVIAL)
        self.assertEqual(code, 4, err)
        self.assertEqual(self.calls(), [], "no model call")
        _refusal_lines(self, err)
        task = self.only_task()
        self.assertEqual(task["refused"]["check"], "no_checks", task)
        self.assertIn("hygiene.commands.repo", task["refused"]["reason"])
        self.assertNotIn("no_checks", task)
        self.assertEqual(self.state_of(doc["task"])["rungs"], [])
        self.assert_schema()

    def test_explain_review_and_plan_are_not_exempt(self):
        for kind in ("explain", "review", "plan"):
            with self.subTest(kind):
                self.cursor({"files": README}, model=GL)
                code, _doc, err = self.run_json("--type", kind, "--difficulty", "trivial")
                self.assertEqual(code, 4, err)
                self.assertEqual(self.calls(), [], "no model call")

    def test_no_checks_runs_as_before_and_records_it(self):
        self.cursor({"files": README}, model=GL)
        code, doc, err = self.run_json("--no-checks", *TRIVIAL)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.models(), [GL])
        task = self.only_task()
        self.assertIs(task["no_checks"], True, task)
        self.assertNotIn("refused", task)
        self.assertEqual(self.state_of(doc["task"])["rungs"][-1]["signals"]["H"], True)
        self.assert_schema()


class TestJudgeOnRefused(NoHygiene):
    """The intake judge is on by default and is itself a model call, so the refusal comes before it."""

    def write_cfg(self):
        super().write_cfg()
        with open(self.cfgpath, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw.pop("judge")
        with open(self.cfgpath, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)

    def test_judge_on_makes_no_intake_call(self):
        self.cursor({"files": README}, model=GL)
        code, _doc, err = self.run_json(*TRIVIAL)
        self.assertEqual(code, 4, err)
        self.assertEqual(self.calls(), [], "no judge or rung call")


class TestOnceRefused(RunCase):
    no_checks = False

    def test_once_exits_4_before_any_model_call(self):
        self.cursor({"files": README})
        code, _out, err = self.mr()
        self.assertEqual(code, 4, err)
        self.assertEqual(self.calls(), [], "no model call")
        _refusal_lines(self, err)

    def test_once_with_no_checks_runs(self):
        self.cursor({"files": README})
        code, _out, err = self.mr("--no-checks")
        self.assertEqual(code, 0, err)
        self.assertEqual([c["model"] for c in self.calls()], [GL])
        self.assertIs(self.only_task()["no_checks"], True)


class TestWithHygiene(LoopCase):
    def test_repo_with_hygiene_commands_runs(self):
        self.cursor({"files": README}, model=GL)
        code, doc, err = self.run_json(*TRIVIAL)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.models(), [GL])
        task = self.only_task()
        self.assertNotIn("refused", task)
        self.assertNotIn("no_checks", task)

    def test_no_checks_on_a_checked_repo_still_runs_hygiene(self):
        self.cursor({"files": README}, model=GL)
        code, doc, err = self.run_json("--no-checks", *TRIVIAL)
        self.assertEqual(code, 0, err)
        self.assertNotIn("no_checks", self.only_task(), "the flag waived nothing")


class TestResumeNotRefused(ResumeCase):
    """A task deferred under --no-checks resumes without the flag and is not newly refused."""
    hygiene_cmds = []

    def test_resume_of_a_no_checks_task_runs_and_keeps_the_record(self):
        self.set_scenario({"cursor": {GL: [STRAYED], GM: [{"files": README}]},
                           "claude": {SONNET: [verdict("accept", WHY_ACCEPT), {"result": REVIEW_PASS}]}},
                          check=DENY)
        code, doc, text = self.go("--no-checks", *EASY)
        self.assertEqual(code, 75, text)
        self.guard(ALLOW)
        code, _doc, text = self.resume(doc["task"])
        self.assertEqual(code, 0, text)
        self.assertNotIn("hygiene.commands.repo", text)
        last = self.records("task")[-1]
        self.assertEqual((last["exit"], last.get("resumed"), last.get("no_checks")), (0, True, True), last)


class TestSetupLine(unittest.TestCase):
    _base = test_setup_mr.SetupTests
    setUp, tearDown, exe, run_mr = _base.setUp, _base.tearDown, _base.exe, _base.run_mr

    def test_setup_tells_the_user_to_add_hygiene_commands(self):
        self.exe("cursor-agent")
        target = self.root / "routes.yaml"
        code, out, err = self.run_mr(["--config", str(target), "setup", "--yes", "--provider", "cursor"])
        self.assertEqual(code, exitcodes.OK, err)
        lines = [ln for ln in out.splitlines() if "hygiene.commands.<repo>" in ln]
        self.assertEqual(len(lines), 1, out)
        self.assertIn("--no-checks", lines[0])
        self.assertEqual(load(str(target)).hygiene["commands"], {}, "setup does not guess commands")


class TestBlankCommandsRefused(LoopCase):
    """no_checks.blank_commands: a blank entry checks nothing, so it does not count as a command."""

    def test_blank_and_space_only_commands_exit_4_before_any_model_call(self):
        for cmds in ([""], ["   "], ["", " \t"]):
            with self.subTest(cmds=cmds):
                self.hygiene_cmds = cmds
                self.write_cfg()
                self.cursor({"files": README}, model=GL)
                code, _doc, err = self.run_json(*TRIVIAL)
                self.assertEqual(code, 4, err)
                self.assertEqual(self.calls(), [], "no model call")
                _refusal_lines(self, err)
                self.assertEqual(self.records("task")[-1]["refused"]["check"], "no_checks")

    def test_blank_commands_run_with_no_checks(self):
        self.hygiene_cmds = [""]
        self.write_cfg()
        self.cursor({"files": README}, model=GL)
        code, _doc, err = self.run_json("--no-checks", *TRIVIAL)
        self.assertEqual(code, 0, err)
        self.assertIs(self.only_task()["no_checks"], True)


class TestMixedCommandsKeepTheRealOne(unittest.TestCase):
    def test_blank_entries_drop_and_the_real_command_still_runs(self):
        from mr import hygiene
        from tests.test_hygiene import _cfg
        tmp = tempfile.mkdtemp(prefix="mr-nochk-")
        self.addCleanup(shutil.rmtree, tmp, True)
        failing = "python3 -c \"print('src/a.ts:1:1: boom'); raise SystemExit(1)\""
        cfg = _cfg(os.path.join(tmp, "home"), {"demo": ["", "   ", failing]})
        self.assertIsNone(hygiene.unchecked(cfg, "demo"))
        sig = hygiene.hygiene(tmp, cfg, frozenset(), "demo")
        self.assertFalse(sig.skipped, sig)
        self.assertFalse(sig.ok, sig)
        self.assertEqual(len(sig.new_failures), 1, sig)
        self.assertIsNotNone(hygiene.unchecked(_cfg(os.path.join(tmp, "home"), {"demo": ["", "  "]}), "demo"))

