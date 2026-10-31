import json
import os
import tempfile
import time
import unittest
from unittest import mock

from mr.trace import load, render, scrub


def rec(**k):
    return json.dumps(k) + "\n"


class Load(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, "log"))
        os.makedirs(os.path.join(self.home, "raw", "t-00000001"))
        with open(os.path.join(self.home, "raw", "t-00000001", "a1-m-low.out"), "w") as f:
            f.write(json.dumps({"type": "result", "result": "no.\nMR_RESULT {\"status\":\"pushback\",\"pushback\":{\"type\":\"scope_needed\"}}",
                                "session_id": "s1", "usage": {"inputTokens": 5, "outputTokens": 2}}))
        lines = [
            rec(role="route", task="t-00000001", ts="2026-10-30T01:00:00+05:30", applied={"route": "cascade", "ladder": ["G-l", "G-h"], "difficulty": "easy"}),
            rec(role="implementer", task="t-00000001", rung="G-l", model="m-low", path="cursor", attempt=1, started_at="2026-10-30T01:00:00+05:30", wall_s=30, decision="stop", failure_class="tried_wrong", signals={"A": False}),
            rec(role="guard", task=None, rung="S", allow=True, reason="ok", ts="2026-10-30T01:00:40+05:30"),
            rec(role="implementer", task="t-00000001", rung="G-h", model="m-high", path="cursor", attempt=2, started_at="2026-10-30T01:00:45+05:30", wall_s=60, decision="accept", signals={"A": True}),
            rec(role="task", task="t-00000001", started_at="2026-10-30T01:00:00+05:30", ended_at="2026-10-30T01:02:00+05:30", wall_s=120, outcome="accepted", repo="secret-repo", ladder=["G-l", "G-h"], cost={"units": 1.5}),
            rec(role="route", task="t-00000002", ts="2026-10-30T02:00:00+05:30", applied={"route": "cascade", "ladder": ["G-h"]}),
        ]
        with open(os.path.join(self.home, "log", "runs.jsonl"), "w") as f:
            f.writelines(lines + ["not json\n"])

    def build(self, live=None):
        with mock.patch.object(load, "running_info", return_value=live or {}):
            return {t["task"]: t for t in load.build_traces(self.home, with_transcripts=False)}

    def test_join_steps_raw_and_guard(self):
        t = self.build()["t-00000001"]
        self.assertEqual([s["rung"] for s in t["steps"]], ["G-l", "G-h"])
        self.assertEqual(t["steps"][1]["escalated_from"], "G-l")
        self.assertEqual(t["steps"][0]["pushback"]["type"], "scope_needed")
        self.assertEqual(t["steps"][0]["tokens"]["in"], 5)
        self.assertEqual(len(t["events"]), 1)
        self.assertEqual(t["status"], "accepted")

    def test_unfinished_task_and_live_step(self):
        self.assertEqual(self.build()["t-00000002"]["status"], "no_end_record")
        t = self.build({"t-00000002": {"role": "planner", "model": "grok-4.7-high-fast", "since": time.time() - 5}})["t-00000002"]
        self.assertEqual(t["status"], "running")
        self.assertTrue(t["steps"][-1]["live"])
        self.assertEqual(t["steps"][-1]["rung"], "G-h")

    def test_page_and_demo_are_clean(self):
        traces = list(self.build().values())
        html = render.page(render.payload(traces))
        self.assertIn("MRTRACE_DATA", html)
        clean = scrub.scrub_traces(traces)
        self.assertNotIn("secret-repo", json.dumps(clean))


if __name__ == "__main__":
    unittest.main()
