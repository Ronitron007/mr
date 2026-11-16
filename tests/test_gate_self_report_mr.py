"""gate.self_report: a blocked or pushback MR_RESULT is never accepted, even with green checks (issue #1 finding 4)."""
import glob
import json
import os
import unittest

from tests import gitfix
from tests.test_aj10_defer_mr import ALLOW, DENY, DeferCase
from tests.test_loop_run import GH, GL, GM, GX, OPUS, PLAN, REVIEW_PASS, SONNET, LoopCase, mul

SCOPE_NEEDED = {"type": "scope_needed", "item": "I1", "evidence": "calc.py:1 cannot finish safely"}


def said(status, pushback=SCOPE_NEEDED):
    return "MR_RESULT " + json.dumps({"status": status, "requirements": [], "deviations": [],
                                      "pushback": pushback, "confidence": 0.1})


class TestCodexRepro(LoopCase):
    """The `incomplete-self-report-accepted` reproduction: easy code task, no hygiene, no planner, no reviewer."""
    repo_files = {"calc.py": "def old():\n    return 1\n"}

    def write_cfg(self):
        raw = {"paths": {"home": self.home, "cursor_agent": gitfix.FAKE_CURSOR,
                         "claude": gitfix.FAKE_CLAUDE, "check": gitfix.FAKE_CHECK}, "judge": {"mode": "off"}}
        with open(self.cfgpath, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)

    def run_status(self, status):
        step = {"result": said(status), "files": {"calc.py": "# Partly completed\ndef old():\n    return 1\n"}}
        self.set_scenario({"cursor": {GH: [step], GM: [step]},
                           "claude": {SONNET: [step], OPUS: [step]}})
        code, doc, err = self.run_json("--type", "code", "--difficulty", "easy", "--rung", "G-h",
                                       text="update calc.py")
        return code, self.state_of(doc["task"]), err

    def test_blocked_comment_only_is_not_accepted(self):
        code, st, err = self.run_status("blocked")
        self.assertNotEqual(code, 0, err)
        self.assertNotEqual(st["outcome"], "accepted")
        self.assertEqual(self.models(), [GH, GM, SONNET],
                         "blocked with evidence is tried_wrong (D-F4): each rung escalates, no review call")
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]],
                         [("G-h", "tried_wrong"), ("G-m", "tried_wrong"), ("S", "tried_wrong")])

    def test_scope_needed_pushback_comment_only_is_not_accepted(self):
        code, st, err = self.run_status("pushback")
        self.assertNotEqual(code, 0, err)
        self.assertNotEqual(st["outcome"], "accepted")
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]], [("G-h", "spec_suspect")])
        self.assertTrue(self.prompts(GH)[1].startswith("You are the planner"), "spec_suspect asks for a re-plan")

    def test_done_comment_only_is_still_accepted(self):
        code, st, err = self.run_status("done")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.models(), [GH])
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]], [("G-h", None)])

    def assert_stopped_as(self, status, rungs):
        code, st, err = self.run_status(status)
        self.assertNotEqual(code, 0, err)
        self.assertNotEqual(st["outcome"], "accepted", f"{status!r} must not be accepted")
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]], rungs)

    def test_title_case_blocked_is_not_accepted(self):
        self.assert_stopped_as("Blocked", [("G-h", "tried_wrong"), ("G-m", "tried_wrong"), ("S", "tried_wrong")])

    def test_upper_case_blocked_is_not_accepted(self):
        self.assert_stopped_as("BLOCKED", [("G-h", "tried_wrong"), ("G-m", "tried_wrong"), ("S", "tried_wrong")])

    def test_pushback_with_a_trailing_space_is_not_accepted(self):
        self.assert_stopped_as("pushback ", [("G-h", "spec_suspect")])

    def test_title_case_done_is_still_accepted(self):
        code, st, err = self.run_status("Done")
        self.assertEqual(code, 0, err)
        self.assertEqual(st["outcome"], "accepted")
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]], [("G-h", None)])


class TestPlannedTask(LoopCase):
    """A medium task with a planner: the patch passes D, H and A, so only the self-report can stop it."""

    def run_medium(self, scenario):
        self.set_scenario(scenario)
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "medium")
        return code, self.state_of(doc["task"]), err

    def test_valid_pushback_replans_once_then_exits_3(self):
        pushback = dict(mul("*"), result=said("pushback"))
        code, st, err = self.run_medium({
            "cursor": {GH: [{"result": json.dumps(PLAN)}, {"result": json.dumps(PLAN)}], GX: [pushback]},
            "claude": {SONNET: [pushback, {"result": REVIEW_PASS}]}})
        self.assertEqual(code, 3, err)
        self.assertEqual(self.models(), [GH, GX, GH, SONNET],
                         "plan, G-x pushback, one re-plan, S pushback, then stop: no reviewer, no third rung")
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]],
                         [("G-x", "spec_suspect"), ("S", "spec_suspect")])
        self.assertEqual(st["rungs"][0]["signals"]["A"], True, "the checks were green")
        self.assertIn("spec_suspect after re-plan", st["reason"])

    def test_done_with_green_checks_is_accepted_on_the_first_rung(self):
        code, st, err = self.run_medium({
            "cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [dict(mul("*"), result=said("done", None))]},
            "claude": {SONNET: [{"result": REVIEW_PASS}]}})
        self.assertEqual(code, 0, err)
        self.assertEqual(self.models(), [GH, GX, SONNET])
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]], [("G-x", None)])
        self.assertEqual(st["outcome"], "accepted")


class TestLegacyResume(DeferCase):
    """A state saved before the status fold keeps `Blocked` or `Pushback` verbatim. A resume must still stop it."""
    check_judge = {"scope": "on"}
    SCOPE_OK = json.dumps({"verdict": "accept", "reason": "ok", "instructions": [], "findings_ok": []})
    CRASH = {"rc": 1, "is_error": True, "result": ""}

    def save_status(self, tid, status):
        path = glob.glob(os.path.join(self.home, "**", tid, "state.json"), recursive=True)[0]
        with open(path, encoding="utf-8") as fh:
            st = json.load(fh)
        pushback = SCOPE_NEEDED if status.lower() == "pushback" else None
        st["pending"]["rr"]["mr_result"] = json.loads(said(status, pushback)[len("MR_RESULT "):])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(st, fh)

    def resume_with(self, tid, status):
        self.save_status(tid, status)
        code, _, text = self.cli("--json", "resume", tid)
        st = self.state_of(tid)
        self.assertNotEqual(st.get("outcome"), "accepted", f"saved {status!r} must not resume to accepted: {text}")
        self.assertNotEqual(code, 0, text)

    def resume_scope(self, status):
        code, doc, text = self.defer_scope(check=DENY)
        self.assertEqual(code, 75, text)
        self.guard(ALLOW)
        readme = {"files": {"README.md": "# test repo\nfixed typo\n"}}
        self.set_scenario({"cursor": {GL: [readme], GM: [readme], GH: [{"result": json.dumps(PLAN)}]},
                           "claude": {SONNET: [{"result": self.SCOPE_OK}, {"result": REVIEW_PASS}]}}, check=ALLOW)
        self.resume_with(doc["task"], status)

    def resume_review(self, status):
        self.set_scenario({"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("*")]},
                           "claude": {SONNET: [self.CRASH, self.CRASH]}})
        code, doc, text = self.go("--type", "bugfix", "--difficulty", "medium")
        self.assertEqual(code, 75, text)
        self.set_scenario({"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("*")], GM: [mul("*")]},
                           "claude": {SONNET: [self.CRASH, self.CRASH, {"result": REVIEW_PASS}, {"result": REVIEW_PASS}],
                                      OPUS: [mul("*")]}})
        self.resume_with(doc["task"], status)

    def test_scope_deferred_title_case_blocked_is_not_accepted(self):
        self.resume_scope("Blocked")

    def test_scope_deferred_title_case_pushback_is_not_accepted(self):
        self.resume_scope("Pushback")

    def test_review_deferred_title_case_blocked_is_not_accepted(self):
        self.resume_review("Blocked")

    def test_review_deferred_title_case_pushback_is_not_accepted(self):
        self.resume_review("Pushback")


if __name__ == "__main__":
    unittest.main()
