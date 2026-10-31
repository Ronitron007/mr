"""COST1 locked checks: task cost counts every Claude call."""
from __future__ import annotations

import json
import os
import unittest

from tests.log_schema_v2 import check as check_schema
from tests.test_loop_run import GH, GX, OPUS, PLAN, REVIEW_PASS, SONNET, LoopCase, mul
from tests.test_run_once import CHECK_DEFAULT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SF_TEXT = "Fix the event stream race: a late event regresses the state machine status in mul.py."
OPUS_USAGE = {"inputTokens": 40000, "outputTokens": 1500, "costUSD": 0.19}


def _probe(probe):
    return {"default": dict(CHECK_DEFAULT), "probe": probe}


class TestCost1Calls(LoopCase):
    def _medium_grok_pass(self, probe):
        self.set_scenario(
            {"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("*")]},
             "claude": {SONNET: [{"result": REVIEW_PASS}]}},
            check=_probe(probe),
        )
        return self.run_json("--type", "bugfix", "--difficulty", "medium")

    def test_reviewer_delta_in_cost_and_row(self):
        code, doc, err = self._medium_grok_pass({"five_hour_pct": 33.0})
        self.assertEqual(code, 0, err)
        task = self.only_task()
        self.assertEqual(task["cost"]["claude_pts"], 3.0)
        row = self.records("reviewer")[0]
        self.assertEqual(row["rung"], "S")
        self.assertIn("claude_pts", row)
        self.assertEqual(row["claude_pts"], 3.0)
        self.assertEqual(row["pts_pre"], 10)
        self.assertEqual(row["pts_post"], 13)
        st = self.state_of(doc["task"])
        self.assertEqual((st["attempts"], st["claude_rungs"]), (1, 0))

    def test_unknown_delta_uses_guard_estimate(self):
        code, _, err = self._medium_grok_pass("fail")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.only_task()["cost"]["claude_pts"], 4)
        row = self.records("reviewer")[0]
        self.assertEqual(row["rung"], "S")
        self.assertIn("claude_pts", row)
        self.assertIsNone(row["claude_pts"])
        self.assertEqual(row["pts_pre"], 10)
        self.assertIsNone(row["pts_post"])

    def test_planner_row_records_points(self):
        self.set_scenario(
            {"cursor": {GX: [mul("*")]}, "claude": {OPUS: [{"result": json.dumps(PLAN)}]}},
            check=_probe({"five_hour_pct": 33.0}),
        )
        code, _, err = self.run_json("--type", "test", "--difficulty", "hard")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.only_task()["cost"]["claude_pts"], 3.0)
        row = self.records("planner")[0]
        self.assertEqual((row["rung"], row["path"]), ("O", "max"))
        self.assertIn("claude_pts", row)
        self.assertEqual(row["claude_pts"], 3.0)
        self.assertEqual(row["pts_pre"], 10)
        self.assertEqual(row["pts_post"], 13)

    def test_planner_unknown_delta_uses_opus_estimate(self):
        self.set_scenario(
            {"cursor": {GX: [mul("*")]}, "claude": {OPUS: [{"result": json.dumps(PLAN)}]}},
            check=_probe("fail"),
        )
        code, _, err = self.run_json("--type", "test", "--difficulty", "hard")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.only_task()["cost"]["claude_pts"], 10)
        row = self.records("planner")[0]
        self.assertIn("claude_pts", row)
        self.assertIsNone(row["claude_pts"])
        self.assertEqual(row["pts_pre"], 10)
        self.assertIsNone(row["pts_post"])
        self.assertEqual(self.state_of(self.only_task()["task"])["claude_rungs"], 0)

    def test_cursor_reviewer_row_null(self):
        self.set_scenario(
            {"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [{"result": REVIEW_PASS}]},
             "claude": {SONNET: [mul("*")]}},
            check=_probe({"five_hour_pct": 33.0}),
        )
        code, _, err = self.run_json("--type", "bugfix", "--difficulty", "medium", "--rung", "S")
        self.assertEqual(code, 0, err)
        impl = self.records("implementer")[0]
        rev = self.records("reviewer")[0]
        self.assertEqual(rev["rung"], "G-x")
        self.assertEqual(rev["path"], "cursor")
        self.assertIn("claude_pts", rev)
        self.assertIsNone(rev["claude_pts"])
        self.assertIsNone(rev["pts_pre"])
        self.assertIsNone(rev["pts_post"])
        self.assertEqual(self.only_task()["cost"]["claude_pts"], impl["claude_pts"])

    def test_cursor_planner_row_null(self):
        code, _, err = self._medium_grok_pass({"five_hour_pct": 33.0})
        self.assertEqual(code, 0, err)
        row = self.records("planner")[0]
        self.assertEqual(row["path"], "cursor")
        self.assertEqual(row["rung"], "G-h")
        self.assertIn("claude_pts", row)
        self.assertIsNone(row["claude_pts"])
        self.assertIsNone(row["pts_pre"])
        self.assertIsNone(row["pts_post"])

    def test_caps_ignore_reviewer_and_planner(self):
        self.set_scenario(
            {"cursor": {GX: [mul("*")]},
             "claude": {OPUS: [{"result": json.dumps(PLAN)}], SONNET: [{"result": REVIEW_PASS}]}},
            check=_probe({"five_hour_pct": 33.0}),
        )
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "hard")
        self.assertEqual(code, 0, err)
        st = self.state_of(doc["task"])
        self.assertEqual([r["rung"] for r in st["rungs"]], ["G-x"])
        self.assertEqual((st["attempts"], st["claude_rungs"]), (1, 0))
        self.assertEqual(self.records("planner")[0]["rung"], "O")
        self.assertEqual(self.records("reviewer")[0]["rung"], "S")
        self.assertEqual(self.only_task()["cost"]["claude_pts"], 6.0)


class TestCost1Advisor(LoopCase):
    def write_cfg(self):
        super().write_cfg()
        with open(self.cfgpath, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["planner"] = {"rungs": {"hard": "G-h", "critical": "G-h"}}
        with open(self.cfgpath, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)

    def setUp(self):
        super().setUp()
        keys = ("DISABLE_TELEMETRY", "CLAUDE_CODE_DISABLE_ADVISOR_TOOL")
        saved = {k: os.environ.get(k) for k in keys}
        for key in keys:
            os.environ.pop(key, None)

        def restore():
            for key, val in saved.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val

        self.addCleanup(restore)

    def test_advisor_counted_once_with_sa_rung(self):
        self.set_scenario(
            {"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [{"result": REVIEW_PASS}]},
             "claude": {SONNET: [dict(mul("*"), advisor=dict(OPUS_USAGE))]}},
            check=_probe("fail"),
        )
        code, doc, err = self.run_json(
            "--type", "bugfix", "--difficulty", "hard", "--rung", "S+", text=SF_TEXT)
        self.assertEqual(code, 0, err)
        st = self.state_of(doc["task"])
        self.assertEqual([r["rung"] for r in st["rungs"]], ["S+A"])
        self.assertEqual((st["attempts"], st["claude_rungs"]), (1, 1))
        claude = [c for c in self.calls() if c.get("cli") == "claude"]
        self.assertEqual(len(claude), 1)
        impl = self.records("implementer")[0]
        self.assertEqual(impl["rung"], "S+A")
        self.assertIsNone(impl["claude_pts"])
        self.assertEqual(impl["pts_pre"], 10)
        self.assertIsNone(impl["pts_post"])
        self.assertEqual(self.only_task()["cost"]["claude_pts"], 12)


class TestCost1Docs(unittest.TestCase):
    def test_schema_requires_claude_pts(self):
        rec = {
            "ts": "2026-10-30T12:00:00+05:30",
            "v": 2,
            "task": "t-fixture",
            "mr_version": "0.1.0",
            "role": "reviewer",
            "rung": "S",
            "model": "claude-sonnet-5-5",
            "path": "max",
            "attempt": 1,
            "started_at": "2026-10-30T11:00:00+05:30",
            "rc": 0,
            "is_error": False,
            "wall_s": 1.0,
            "signals": {},
            "decision": "accept",
            "failure_class": None,
            "isolation": "W7",
            "patch": None,
            "diff": None,
            "mr_result": None,
            "raw": None,
            "pts_pre": 10,
            "pts_post": 13,
            "usd_equiv": None,
            "model_served": None,
            "item": None,
        }
        missing = check_schema(rec)
        self.assertTrue(any("claude_pts" in item for item in missing), missing)
        self.assertEqual(check_schema({**rec, "claude_pts": 3.0, "role": "planner"}), [])
        self.assertEqual(check_schema({**rec, "claude_pts": None, "role": "implementer"}), [])
        bad = check_schema({**rec, "claude_pts": "3"})
        self.assertTrue(any("claude_pts" in item for item in bad), bad)

    def test_cost_all_claude_module_present(self):
        path = os.path.join(ROOT, "tests", "test_cost_all_claude.py")
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("def test_", text)
        self.assertIn("claude_pts", text)


if __name__ == "__main__":
    unittest.main()
