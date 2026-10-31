"""T20b: the advisor (S+A) wired into loop.run_task (advisor-consult-spec §3-§9; AC-ADV-6, -10 via the loop)."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from mr import cli, isolation
from tests.gitfix import git
from tests.test_loop_run import GH, GX, PLAN, REVIEW_PASS, LoopCase, mul
from tests.test_run_once import CHECK_DEFAULT

SF_TEXT = "Fix the event stream race: a late event regresses the state machine status in mul.py."
OPUS_USAGE = {"inputTokens": 40000, "outputTokens": 1500, "costUSD": 0.19}


def block_result():
    relay = "MR_ADVISOR " + json.dumps({
        "slot": "early", "verdict": "block",
        "risks": [{"what": "a late event still clears the terminal state", "where": "mul.py:1",
                   "severity": "high", "test": "apply error then running"}],
        "missing_test": None, "confidence": 0.9, "approach": "change", "relayed": "verbatim"})
    return ("changed approach and finished\n" + relay + "\n"
            'MR_RESULT {"item":null,"status":"done","requirements":[],"deviations":[],'
            '"pushback":null,"confidence":0.8}')


class AdvisorLoopCase(LoopCase):
    def write_cfg(self):
        # hard/critical plan on O by default; these tests are about S+A, so keep the planner on G-h
        super().write_cfg()
        with open(self.cfgpath, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["planner"] = {"rungs": {"hard": "G-h", "critical": "G-h"}}
        with open(self.cfgpath, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)

    def setUp(self):
        super().setUp()
        saved = {k: os.environ.get(k) for k in ("DISABLE_TELEMETRY", "CLAUDE_CODE_DISABLE_ADVISOR_TOOL")}
        for key in saved:
            os.environ.pop(key, None)
        self.addCleanup(os.environ.update, {k: v for k, v in saved.items() if v is not None})

    def claude_calls(self):
        return [c for c in self.calls() if c["cli"] == "claude"]

    def advisor_records(self):
        return [r for r in self.log() if r["role"] == "advisor"]


class TestAdvisorRungs(AdvisorLoopCase):
    def test_hard_sf_task_launches_sa(self):  # AC-ADV-10 via the loop
        self.set_scenario(
            {"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [{"result": REVIEW_PASS}]},
             "claude": {"claude-sonnet-5-5": [dict(mul("*"), advisor=dict(OPUS_USAGE))]}})
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "hard", "--rung", "S+",
                                       text=SF_TEXT)
        self.assertEqual(code, 0, err)
        calls = self.claude_calls()
        self.assertEqual(len(calls), 1)
        argv = calls[0]["argv"]
        self.assertEqual(argv[argv.index("--advisor") + 1], "claude-opus-5-5")
        self.assertIn("stream-json", argv)
        self.assertIn("--append-system-prompt", argv)
        self.assertNotIn("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", calls[0]["env"])
        self.assertIn("--- mr advisor protocol adv.v2 (consults allowed: 1) ---", calls[0]["prompt"])
        self.assertIn("After your tests pass, if your diff touches any of:", calls[0]["prompt"])  # Y3 final slot
        self.assertIn("mul.py", calls[0]["prompt"])
        rec = self.advisor_records()
        self.assertEqual(len(rec), 1)
        rec = rec[0]
        self.assertEqual((rec["row"], rec["gate"], rec["slots"]), ("Y3", "ok", ["final"]))
        self.assertEqual((rec["consult_made"], rec["relay_status"]), (1, "missing"))
        self.assertEqual((rec["advisor_in_tok"], rec["advisor_out_tok"]), (40000, 1500))
        self.assertAlmostEqual(rec["advisor_usd"], 0.19)
        st = self.state_of(doc["task"])
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]], [("S+A", None)])
        self.assertEqual(st["claude_rungs"], 1)  # S+A counts as a Claude (Sonnet) rung
        self.assertEqual(doc["outcome"], "accepted")  # relay missing: the rung result is unchanged

    def test_easy_task_runs_plain_splus(self):  # N5
        self.set_scenario({"claude": {"claude-sonnet-5-5": [
            {"files": {"README.md": "# test repo\nfixed typo\n"}}]}})
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "easy", "--rung", "S+")
        self.assertEqual(code, 0, err)
        calls = self.claude_calls()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--advisor", calls[0]["argv"])
        self.assertEqual(calls[0]["env"].get("CLAUDE_CODE_DISABLE_ADVISOR_TOOL"), "1")
        self.assertNotIn("mr advisor protocol", calls[0]["prompt"])
        self.assertEqual([r["rung"] for r in self.state_of(doc["task"])["rungs"]], ["S+"])
        self.assertEqual(self.advisor_records(), [])

    def test_guard_fits_6_not_12_runs_plain_splus(self):  # §4 G1 -> gate budget
        self.set_scenario(
            {"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [{"result": REVIEW_PASS}]},
             "claude": {"claude-sonnet-5-5": [mul("*")]}},
            check={"default": dict(CHECK_DEFAULT, hour_usage=24)})
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "hard", "--rung", "S+",
                                       text=SF_TEXT)
        self.assertEqual(code, 0, err)
        calls = self.claude_calls()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--advisor", calls[0]["argv"])
        self.assertEqual(calls[0]["env"].get("CLAUDE_CODE_DISABLE_ADVISOR_TOOL"), "1")
        rec = self.advisor_records()
        self.assertEqual(len(rec), 1)
        self.assertEqual((rec[0]["gate"], rec[0]["row"]), ("budget", "Y3"))
        self.assertTrue(rec[0]["consult_requested"])
        self.assertEqual(rec[0]["consult_made"], 0)
        self.assertEqual([r["rung"] for r in self.state_of(doc["task"])["rungs"]], ["S+"])

    def test_g6_uses_advisor_child_env(self):
        """G6 reads the S+A child env. The box exports both keys on the mr process."""
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1",
                                          "DISABLE_TELEMETRY": "1"}):
            self.set_scenario(
                {"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [{"result": REVIEW_PASS}]},
                 "claude": {"claude-sonnet-5-5": [
                     dict(mul("*"), advisor=dict(OPUS_USAGE)),
                     {"files": {"README.md": "# test repo\nfixed typo\n"}}]}})
            code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "hard", "--rung", "S+",
                                           text=SF_TEXT)
            self.assertEqual(code, 0, err)
            sa = self.claude_calls()
            self.assertEqual(len(sa), 1)
            self.assertIn("--advisor", sa[0]["argv"])
            self.assertNotIn("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", sa[0]["env"])
            self.assertNotIn("DISABLE_TELEMETRY", sa[0]["env"]["env_keys"])
            self.assertNotIn("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", sa[0]["env"]["env_keys"])
            git(self.repo, "reset", "--hard", "HEAD")
            git(self.repo, "clean", "-fd")
            code, doc, err = self.run_json("--type", "docs", "--difficulty", "easy", "--rung", "S+")
            self.assertEqual(code, 0, err)
            plain = self.claude_calls()
            self.assertEqual(len(plain), 2)
            self.assertNotIn("--advisor", plain[1]["argv"])
            self.assertEqual(plain[1]["env"].get("CLAUDE_CODE_DISABLE_ADVISOR_TOOL"), "1")

    def test_block_relayed_checks_pass_critical_exit3(self):  # AC-ADV-6: one §6.3 row
        self.set_scenario(
            {"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [{"result": REVIEW_PASS}]},
             "claude": {"claude-sonnet-5-5": [
                 dict(mul("*"), advisor=dict(OPUS_USAGE), result=block_result())]}})
        # critical forces W4 (worktrees, not built in v1) before any rung; force W7 to reach the loop
        with mock.patch.object(isolation, "decide_isolation", return_value="W7"):
            code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "critical",
                                           "--rung", "S+", text=SF_TEXT)
        self.assertEqual(code, 3, err)
        self.assertEqual(doc["outcome"], "needs_human")
        self.assertTrue(os.path.isfile(doc["patch"]))  # exit 3 with the patch saved
        calls = self.claude_calls()
        self.assertEqual(len(calls), 1)  # a block never schedules an O rung by itself
        self.assertEqual(calls[0]["model"], "claude-sonnet-5-5")
        st = self.state_of(doc["task"])
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]], [("S+A", None)])
        self.assertEqual(st["used_rungs"], ["S+A"])
        self.assertTrue(st["rungs"][0]["signals"]["C"])  # C was required and ran on the risks
        rec = self.advisor_records()
        self.assertEqual(len(rec), 1)
        self.assertEqual((rec[0]["verdict"], rec[0]["relay_status"]), ("block", "ok"))
        self.assertEqual(rec[0]["risks"], [{"where": "mul.py:1", "severity": "high"}])


class TestAdvisorCliFlags(unittest.TestCase):
    def test_flags_parse_and_validate(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli.main(["route", "--advisor", "force", "--advisor-consults", "2",
                      "--advisor-slot", "final"])
        self.assertNotIn("invalid choice", err.getvalue())  # the flags parse (US-2 here is the missing task)
        for argv in (["run", "--advisor", "bogus"], ["run", "--advisor-consults", "3"],
                     ["run", "--advisor-slot", "middle"]):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(argv), 2, argv)
            self.assertIn("--advisor", err.getvalue(), argv)


if __name__ == "__main__":
    unittest.main()
