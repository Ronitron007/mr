"""T18: §14.3 scenario matrix S1–S9, AC-CLI-3 exit codes, AC-STATE-1/2, §0.2/§0.3 static checks.

All runs drive ``mr.cli.main([...])`` (or a real ``python -m mr`` subprocess for signals)
against temp gitfix repos with MR_FAKE_SCENARIO / MR_FAKE_CHECK, as in test_loop_run.py.
No real model is ever invoked.
"""
import ast
import contextlib
import io
import json
import os
import re
import signal
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import patch

from mr import acceptance, cli
from tests import gitfix
from tests.gitfix import git
from tests.test_loop_run import (GH, GL, GM, GX, PLAN, README, RED, REVIEW_PASS, TEST_PATH,
                                 LoopCase, mul as mul_file)
from tests.test_run_once import TASK, _wait

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MR_SRC = os.path.join(SRC, "mr")
C25 = "composer-2.5"
SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"


class TestScenarioMatrix(LoopCase):
    """§14.3 S1–S9 plus the two exit-code producers (1, 2) the matrix itself does not cover."""

    # -- scenarios (each returns the mr exit code) -----------------------------

    def _s1_trivial_docs_pass(self):
        self.cursor({"files": README}, model=GL)
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.models(), [GL])
        st = self.state_of(doc["task"])
        self.assertEqual((st["isolation"], st["next"], st["outcome"]), ("W7", "done", "accepted"))
        self.assertEqual([r["rung"] for r in st["rungs"]], ["G-l"])
        self.assertEqual(doc["rungs"], [{"rung": "G-l", "class": None,
                                         "signals": {"H": True, "D": True, "A": None, "C": None}}])
        return code

    def _s2_medium_tried_wrong_then_sonnet(self):
        self.set_scenario({
            "cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul_file("+"), {"result": REVIEW_PASS}]},
            "claude": {SONNET: [mul_file("*", "  # fixed")]}})
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "medium")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.models(), [GH, GX, SONNET, GX])  # planner, G-x, S, G-x reviews S
        st = self.state_of(doc["task"])
        self.assertEqual([r["rung"] for r in st["rungs"]], ["G-x", "S"])
        self.assertEqual([r["class"] for r in st["rungs"]], ["tried_wrong", None])
        self.assertEqual(st["rungs"][1]["signals"], {"H": True, "D": True, "A": True, "C": True})
        self.assertEqual(st["used_rungs"], ["G-x", "S"])
        return code

    def _s3_skipped_effort_up(self):
        self.set_scenario({"cursor": {GM: [{}], GH: [{"files": README}]}})
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial", "--rung", "G-m")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.models(), [GM, GH])  # effort raised once: G-m -> G-h
        st = self.state_of(doc["task"])
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]], [("G-m", "skipped"), ("G-h", None)])
        self.assertEqual(self.only_task()["esc"]["skipped"], 1)
        return code

    def _s4_d_fail_reset_then_next_rung(self):
        stray = {"sub/other.txt": "x\n"}
        self.set_scenario({"cursor": {
            GH: [{"result": json.dumps(PLAN)}],
            GX: [{"files": {**stray, **mul_file("*")["files"]}}, {"result": REVIEW_PASS}]},
            "claude": {SONNET: [mul_file("*", "  # fixed")]}})
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "medium")
        self.assertEqual(code, 0, err)
        st = self.state_of(doc["task"])
        self.assertEqual([r["class"] for r in st["rungs"]], ["off_track", None])
        self.assertEqual(st["rungs"][0]["signals"]["D"], False)
        # reset restored the tree to reset_tree: the stray file is gone...
        self.assertFalse(os.path.exists(os.path.join(self.repo, "sub", "other.txt")))
        self.assertIn("was reset to the start of the task", self.prompts(SONNET)[0])
        # ...and the locked tests survived the reset (verify_lock ok).
        with open(os.path.join(self.repo, TEST_PATH), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), RED)
        with open(os.path.join(self.home, "tasks", doc["task"], "tests.lock"), encoding="utf-8") as fh:
            self.assertEqual(acceptance.verify_lock(self.repo, json.load(fh)), [])
        return code

    def _s5_two_models_same_a_spec_suspect_replan_exit3(self):
        wrong = [mul_file("+"), mul_file("+", "  # g"), mul_file("+", "  # s"), mul_file("+", "  # o")]
        self.set_scenario({"cursor": {GH: [{"result": json.dumps(PLAN)}], C25: [wrong[0]], GX: [wrong[1]]},
                           "claude": {SONNET: [wrong[2]], OPUS: [wrong[3]]}})
        # --rung C25 puts a Composer rung first: the medium ladder has too few distinct models otherwise
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "medium", "--rung", "C25")
        self.assertEqual(code, 3, err)
        # planner, C25, G-x (spec_suspect), one re-plan, then S and O fail the same A item -> exit 3
        self.assertEqual(self.models(), [GH, C25, GX, GH, SONNET, OPUS])
        self.assertEqual([r["class"] for r in doc["rungs"]],
                         ["tried_wrong", "spec_suspect", "tried_wrong", "spec_suspect"])
        self.assertEqual(self.state_of(doc["task"])["replans"], 1)
        self.assertEqual(doc["outcome"], "needs_human")
        return code

    def _s6_claude_denied_fallback_75(self):
        self.set_scenario(
            {"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul_file("+")]},
             "claude": {SONNET: [{}]}},
            check={"default": {"hour_usage": 10, "limit": 40, "unit": "points", "exceeded": False,
                               "five_hour_pct": 84.0, "five_hour_resets_at": "2026-10-30T05:00:00Z",
                               "seven_day_pct": 12.0, "sample_age_s": 30, "samples_in_hour": 3,
                               "has_baseline": True, "checked_at": "2026-10-30T00:00:00Z"}})
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "medium", "--rung", "S")
        self.assertEqual(code, 75, err)
        # Claude never ran: S denied -> fallback G-x; O is denied too and G-x is used, so exit 75
        self.assertEqual(self.models(), [GH, GX])
        self.assertEqual([r["class"] for r in doc["rungs"]], ["guard_deny", "tried_wrong"])
        self.assertTrue(doc["frees_hint"])
        self.assertTrue(os.path.isfile(doc["patch"]))  # a patch is saved
        with open(doc["patch"], encoding="utf-8") as fh:
            self.assertIn("a + b", fh.read())
        # Hard rule §0.3: no cursor-agent invocation ever used a claude-* model.
        bad = [c["argv"] for c in self.calls()
               if c["cli"] == "cursor" and str(c["model"]).startswith("claude-")]
        self.assertEqual(bad, [])
        return code

    def _s7_branch_switch_isolation_violation(self):
        self.cursor({"files": README, "violation": "switch"}, model=GL)
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 4, err)
        self.assertEqual(doc["outcome"], "unsafe")
        self.assertEqual(doc["rungs"][0]["class"], "isolation_violation")
        st = self.state_of(doc["task"])
        self.assertEqual((st["isolation"], st["exit"]), ("W7", 4))
        return code

    def _s8_on_main_w5_branch_trailer(self):
        self.cursor({"files": README}, model=GL)
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 0, err)
        self.assertTrue(doc["branch"].startswith("mr/"), doc["branch"])
        self.assertEqual(self.state_of(doc["task"])["isolation"], "W5")
        msg = git(self.repo, "log", "-1", "--format=%B").stdout
        self.assertIn(f"Mr-Task: {doc['task']}", msg)
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, "")
        return code

    def _s9_foreign_dirty_exit4(self):
        gitfix.make_foreign_dirty(self.repo)
        self.cursor({"files": README}, model=GL)
        code, _, err = self.run_json("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 4, err)
        self.assertIn("W2", err)
        self.assertEqual(self.calls(), [])
        return code

    def _x1_ladder_exhausted_exit1(self):
        self.set_scenario({"cursor": {GL: [{}], GM: [{}]}})  # empty diffs everywhere
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 1, err)
        self.assertEqual(doc["outcome"], "exhausted")
        self.assertEqual([r["class"] for r in doc["rungs"]], ["skipped", "skipped"])
        return code

    def _x2_usage_error_exit2(self):
        with patch.dict(os.environ, self.env), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--config", self.cfgpath, "run", TASK, "--repo", self.repo,
                             "--rung", "Z9"])
        self.assertEqual(code, 2)
        self.assertEqual(self.calls(), [])
        return code

    def test_scenarios(self):
        cases = [("S1", self._s1_trivial_docs_pass),
                 ("S2", self._s2_medium_tried_wrong_then_sonnet),
                 ("S3", self._s3_skipped_effort_up),
                 ("S4", self._s4_d_fail_reset_then_next_rung),
                 ("S5", self._s5_two_models_same_a_spec_suspect_replan_exit3),
                 ("S6", self._s6_claude_denied_fallback_75),
                 ("S7", self._s7_branch_switch_isolation_violation),
                 ("S8", self._s8_on_main_w5_branch_trailer),
                 ("S9", self._s9_foreign_dirty_exit4),
                 ("X1", self._x1_ladder_exhausted_exit1),
                 ("X2", self._x2_usage_error_exit2)]
        expected = {"S1": 0, "S2": 0, "S3": 0, "S4": 0, "S5": 3, "S6": 75,
                    "S7": 4, "S8": 0, "S9": 4, "X1": 1, "X2": 2}
        seen = {}
        for name, fn in cases:
            with self.subTest(scenario=name):
                self.branch = "main" if name == "S8" else "feature/x"
                self.setUp()  # fresh repo/home/scenario per scenario
                seen[name] = fn()
        self.assertEqual(seen, expected)
        # AC-CLI-3: every code but 130 (SIGINT, see TestSigint130) is produced above.
        self.assertTrue({0, 1, 2, 3, 4, 75} <= set(seen.values()), seen)


class _Subprocess:
    """Real ``python -m mr`` processes for signal tests (pattern from test_run_once)."""

    def spawn(self, *extra, text, scale="1"):
        env = dict(self.env, MR_TEST_TIME_SCALE=scale)
        return subprocess.Popen(
            [sys.executable, "-m", "mr", *self.argv(*extra, text=text)], cwd=SRC, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def cmdlines_with(self, marker):
        hits = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == os.getpid():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as fh:
                    if marker.encode() in fh.read():
                        hits.append(pid)
            except OSError:
                pass
        return hits

    def reap(self, marker):
        for pid in self.cmdlines_with(marker):
            with contextlib.suppress(OSError):
                os.kill(int(pid), signal.SIGKILL)


class TestSigint130(_Subprocess, LoopCase):
    def test_sigint_exit_130(self):
        """AC-CLI-3: SIGINT to a subprocess `python -m mr run ...` while a fake rung sleeps."""
        marker = "ZZE2ESIGINT" + uuid.uuid4().hex[:8]
        self.cursor({"files": README, "sleep_s": 120}, model=GL)
        proc = self.spawn("--type", "docs", "--difficulty", "trivial", text=f"{TASK} {marker}")
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.addCleanup(self.reap, marker)
        self.assertTrue(_wait(lambda: bool(self.calls())), "fake cursor never started")
        time.sleep(0.5)
        proc.send_signal(signal.SIGINT)
        out, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 130, err)
        doc = json.loads(out)
        self.assertEqual((doc["exit"], doc["outcome"]), (130, "interrupted"))
        self.assertEqual(self.only_task()["exit"], 130)
        self.assertEqual(self.cmdlines_with(marker), [], "fake process group survived SIGINT")
        self.assertEqual(os.path.getsize(self.lock_path()), 0)


class TestCrashState(_Subprocess, LoopCase):
    """AC-STATE-1/2: SIGKILL after rung 2; stale lock -> exit 4 -> `mr unlock --repo` -> next run."""

    def _crash_after_rung2(self):
        """easy bugfix ladder [G-h, G-x, S]: G-h/G-x go off_track (reset), S sleeps; SIGKILL it."""
        marker = "ZZE2ECRASH" + uuid.uuid4().hex[:8]
        stray = [{"files": {"other/zzz.txt": "z\n"}}]
        self.set_scenario({"cursor": {GH: stray, GX: stray},
                           "claude": {SONNET: [{"sleep_s": 120}]}})
        proc = self.spawn("--type", "bugfix", "--difficulty", "easy",
                          text=f"fix typo in README.md {marker}")
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        self.addCleanup(self.reap, marker)
        self.assertTrue(_wait(lambda: len(self.calls()) >= 3), "rung 3 (S) never started")
        proc.kill()
        proc.communicate(timeout=30)
        self.reap(marker)
        tasks = os.listdir(os.path.join(self.home, "tasks"))
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    def test_kill_after_rung2(self):
        """AC-STATE-1: state.json exists and its rungs match the log's implementer records."""
        tid = self._crash_after_rung2()
        st = self.state_of(tid)  # state.json exists
        impl = [r for r in self.log() if r["role"] == "implementer"]
        self.assertEqual(len(impl), 2, self.log())
        self.assertEqual([(r["rung"], r["class"], r["model"], r["signals"]) for r in st["rungs"]],
                         [(r["rung"], r["failure_class"], r["model"], r["signals"]) for r in impl])
        self.assertEqual([r["rung"] for r in st["rungs"]], ["G-h", "G-x"])
        self.assertEqual(st["next"], "rung")  # killed inside rung 3
        self.assertEqual([r for r in self.log() if r["role"] == "task"], [])  # no finalize

    def test_stale_lock(self):
        """AC-STATE-2: next run exits 4 naming the task; `mr unlock --repo` -> 0; next run proceeds."""
        tid = self._crash_after_rung2()
        ncalls = len(self.calls())
        code, _, err = self.mr("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 4)
        self.assertIn(tid, err)
        self.assertEqual(len(self.calls()), ncalls)  # no model ran behind the stale lock

        out = io.StringIO()
        with patch.dict(os.environ, self.env), contextlib.redirect_stdout(out):
            code = cli.main(["--config", self.cfgpath, "unlock", "--repo", self.repo])
        self.assertEqual(code, 0)
        self.assertIn(tid, out.getvalue())
        self.assertEqual(os.path.getsize(self.lock_path()), 0)

        self.set_scenario({"cursor": {GL: [{"files": README}]}})
        code, _, err = self.mr("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 0, err)  # the next run proceeds
        last = self.records("task")[-1]  # the stale-lock run left its own task record
        self.assertEqual(self.state_of(last["task"])["outcome"], "accepted")
        self.assertEqual(last["exit"], 0)


def _mr_py_files():
    for root, _, names in os.walk(MR_SRC):
        for name in sorted(names):
            if name.endswith(".py"):
                yield os.path.join(root, name)


class TestStaticHardRules(unittest.TestCase):
    """§0.2 (no judge built) and §0.3 (no claude-* model via cursor-agent), checked statically."""

    def test_no_judge_import(self):
        pat = re.compile(r"^\s*(?:import\s+mr\.judge\b|from\s+mr\.judge\b|"
                         r"from\s+mr\s+import\s+[^#\n]*\bjudge\b)", re.M)
        for path in _mr_py_files():
            with open(path, encoding="utf-8") as fh:
                self.assertIsNone(pat.search(fh.read()), f"judge import in {path}")
        self.assertFalse(os.path.isfile(os.path.join(MR_SRC, "judge.py")))

    def test_no_claude_literal_to_run_cursor(self):
        for path in _mr_py_files():
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            self.assertIsNone(re.search(r"run_cursor\(\s*[\"']claude-", src),
                              f"claude-* literal passed to run_cursor in {path}")
            for node in ast.walk(ast.parse(src, filename=path)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute) else None)
                if name != "run_cursor":
                    continue
                candidates = list(node.args) + [kw.value for kw in node.keywords if kw.arg == "model"]
                for arg in candidates:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        self.assertFalse(arg.value.startswith("claude-"),
                                         f"claude-* literal {arg.value!r} to run_cursor in {path}")


if __name__ == "__main__":
    unittest.main()
