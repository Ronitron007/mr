"""Task cost counts every Claude call: reviewer, planner, and the S+A advisor consult.

Drives the cases through tests/fakes (the scenario CLIs behind LoopCase).
"""
from __future__ import annotations

import json
import os
import unittest

from tests import gitfix
from tests.test_loop_run import GH, GX, OPUS, PLAN, REVIEW_PASS, SONNET, LoopCase, mul
from tests.test_run_once import CHECK_DEFAULT

OPUS_USAGE = {"inputTokens": 40000, "outputTokens": 1500, "costUSD": 0.19}
SF_TEXT = "Fix the event stream race: a late event regresses the state machine status in mul.py."


def _probe(probe):
    return {"default": dict(CHECK_DEFAULT), "probe": probe}


class TestCostAllClaude(LoopCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(gitfix.FAKE_CLAUDE.endswith(os.path.join("tests", "fakes", "fake_claude.py")))
        self.assertTrue(gitfix.FAKE_CURSOR.endswith(os.path.join("tests", "fakes", "fake_cursor_agent.py")))

    def test_reviewer_measured_delta(self):
        self.set_scenario(
            {"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("*")]},
             "claude": {SONNET: [{"result": REVIEW_PASS}]}},
            check=_probe({"five_hour_pct": 33.0}),
        )
        code, _, err = self.run_json("--type", "bugfix", "--difficulty", "medium")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.only_task()["cost"]["claude_pts"], 3.0)
        row = self.records("reviewer")[0]
        self.assertEqual(row["claude_pts"], 3.0)
        self.assertEqual(row["pts_pre"], 10)
        self.assertEqual(row["pts_post"], 13)
        self.assertEqual(self.records("planner")[0]["claude_pts"], None)

    def test_reviewer_unknown_delta_uses_sonnet_estimate(self):
        self.set_scenario(
            {"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("*")]},
             "claude": {SONNET: [{"result": REVIEW_PASS}]}},
            check=_probe("fail"),
        )
        code, _, err = self.run_json("--type", "bugfix", "--difficulty", "medium")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.only_task()["cost"]["claude_pts"], 4)
        row = self.records("reviewer")[0]
        self.assertIsNone(row["claude_pts"])
        self.assertEqual(row["pts_pre"], 10)
        self.assertIsNone(row["pts_post"])

    def test_planner_unknown_delta_uses_opus_estimate(self):
        self.set_scenario(
            {"cursor": {GX: [mul("*")]}, "claude": {OPUS: [{"result": json.dumps(PLAN)}]}},
            check=_probe("fail"),
        )
        code, _, err = self.run_json("--type", "test", "--difficulty", "hard")
        self.assertEqual(code, 0, err)
        task = self.only_task()
        self.assertEqual(task["cost"]["claude_pts"], 10)
        row = self.records("planner")[0]
        self.assertEqual(row["rung"], "O")
        self.assertIsNone(row["claude_pts"])
        self.assertEqual(row["pts_pre"], 10)
        self.assertIsNone(row["pts_post"])
        self.assertEqual(self.state_of(task["task"])["claude_rungs"], 0)

    def test_cursor_reviewer_adds_no_claude_points(self):
        self.set_scenario(
            {"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [{"result": REVIEW_PASS}]},
             "claude": {SONNET: [mul("*")]}},
            check=_probe({"five_hour_pct": 33.0}),
        )
        code, _, err = self.run_json("--type", "bugfix", "--difficulty", "medium", "--rung", "S")
        self.assertEqual(code, 0, err)
        rev = self.records("reviewer")[0]
        self.assertEqual(rev["path"], "cursor")
        self.assertIsNone(rev["claude_pts"])
        self.assertIsNone(rev["pts_pre"])
        self.assertIsNone(rev["pts_post"])
        self.assertEqual(self.only_task()["cost"]["claude_pts"], self.records("implementer")[0]["claude_pts"])


class TestAdvisorCountedOnce(LoopCase):
    def write_cfg(self):
        super().write_cfg()
        with open(self.cfgpath, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["planner"] = {"rungs": {"hard": "G-h", "critical": "G-h"}}
        with open(self.cfgpath, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)

    def test_sa_consult_is_the_one_claude_call(self):
        self.set_scenario(
            {"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [{"result": REVIEW_PASS}]},
             "claude": {SONNET: [dict(mul("*"), advisor=dict(OPUS_USAGE))]}},
            check=_probe("fail"),
        )
        code, _, err = self.run_json(
            "--type", "bugfix", "--difficulty", "hard", "--rung", "S+", text=SF_TEXT)
        self.assertEqual(code, 0, err)
        claude = [c for c in self.calls() if c.get("cli") == "claude"]
        self.assertEqual(len(claude), 1)
        impl = self.records("implementer")[0]
        self.assertEqual(impl["rung"], "S+A")
        self.assertIsNone(impl["claude_pts"])
        self.assertEqual(self.only_task()["cost"]["claude_pts"], 12)
        st = self.state_of(self.only_task()["task"])
        self.assertEqual((st["attempts"], st["claude_rungs"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
