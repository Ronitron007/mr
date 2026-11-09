"""gate.review_contract: mr derives signal C from the reviewer's items, not from its overall (issue #1 findings 3, 8)."""
import json
import unittest

from mr import review
from tests.test_loop_run import GH, GX, PLAN, REVIEW_PASS, SONNET, LoopCase, mul
from tests.test_review import ReviewCase

R1 = [{"id": "R1", "text": "must return 2"}]
CRASH = {"rc": 1, "is_error": True, "result": ""}
TASK_TEXT = "add mul to mul.py and keep the README typo fix"


def mr_review(items, overall="pass"):
    return "notes\nMR_REVIEW " + json.dumps({"items": items, "overall": overall})


def item(id_, verdict, evidence="mul.py:2"):
    return {"id": id_, "verdict": verdict, "evidence": evidence}


class TestVerdictFromItems(ReviewCase):
    def run_reqs(self, replies, reqs=R1, risk="medium"):
        self.setup({"claude": {SONNET: [{"result": t} for t in replies]}})
        return review.review("diff", reqs, None, "grok-4.7-high", risk, cfg=self.cfg,
                             tree=self.repo, task_id="t1", attempt=1, task_text=TASK_TEXT)

    def test_empty_items_with_pass_is_not_ok(self):
        sig = self.run_reqs([mr_review([]), mr_review([])])
        self.assertFalse(sig.ok, "an empty item list covers no requirement")
        self.assertEqual(sig.attempts, 2, "a reply that misses a requirement id is re-asked once")

    def test_failed_item_with_overall_pass_fails(self):
        sig = self.run_reqs([mr_review([item("R1", "fail", "calc.py is wrong")])])
        self.assertEqual(sig.verdict, "fail")
        self.assertFalse(sig.ok)

    def test_missing_extra_or_duplicate_id_is_reasked(self):
        reqs = R1 + [{"id": "R2", "text": "keeps order"}]
        for bad in ([item("R1", "pass")],
                    [item("R1", "pass"), item("R2", "pass"), item("R3", "pass")],
                    [item("R1", "pass"), item("R1", "pass")]):
            with self.subTest(bad=bad):
                sig = self.run_reqs([mr_review(bad), mr_review([item("R2", "pass"), item("R1", "pass")])],
                                    reqs=reqs)
                self.assertEqual((sig.verdict, sig.ok, sig.attempts), ("pass", True, 2),
                                 "items in another order still cover the ids")

    def test_trailing_note_after_the_line_still_parses(self):
        sig = self.run_reqs([mr_review([item("R1", "pass")]) + "\nthanks, that is all\n"])
        self.assertEqual((sig.verdict, sig.ok, sig.attempts), ("pass", True, 1))

    def test_all_items_pass_and_overall_unclear_is_unclear(self):
        sig = self.run_reqs([mr_review([item("R1", "pass")], "unclear")], risk="low")
        self.assertEqual(sig.verdict, "unclear")
        self.assertTrue(sig.ok, "a valid explicit unclear keeps the low-risk policy")
        self.assertTrue(sig.warning)

    def test_plan_less_needs_one_item(self):
        sig = self.run_reqs([mr_review([]), mr_review([item("TASK", "fail", "mul.py:2 adds")])], reqs=None)
        self.assertEqual((sig.verdict, sig.ok, sig.attempts), ("fail", False, 2))

    def test_two_crashes_at_low_risk_are_not_accepted(self):
        self.setup({"claude": {SONNET: [CRASH, CRASH]}})
        sig = review.review("diff", None, None, "grok-4.7-high", "low", cfg=self.cfg,
                            tree=self.repo, task_id="t1", attempt=1, task_text=TASK_TEXT)
        self.assertFalse(sig.ok, "a crashed reviewer is no verdict")
        self.assertNotEqual(sig.verdict, "unclear")
        self.assertEqual(sig.attempts, 2)


class TestPromptHoldsTask(unittest.TestCase):
    def test_plan_less_prompt_contains_task_text(self):
        prompt = review._prompt("some diff", None, None, "t-id", TASK_TEXT)
        self.assertIn(TASK_TEXT, prompt)
        self.assertIn('"TASK"', prompt)

    def test_checklist_prompt_contains_task_text_too(self):
        prompt = review._prompt("some diff", R1, None, "t-id", TASK_TEXT)
        self.assertIn(TASK_TEXT, prompt)
        self.assertIn("must return 2", prompt)


class TestRunEndToEnd(LoopCase):
    def run_medium(self, scenario):
        self.set_scenario(scenario)
        return self.run_json("--type", "bugfix", "--difficulty", "medium", text=TASK_TEXT)

    def test_reviewer_fails_r1_with_overall_pass_is_not_accepted(self):
        lying = {"result": mr_review([item("R1", "fail", "mul.py:2 wrong")])}
        code, doc, err = self.run_medium({
            "cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("*"), {"result": REVIEW_PASS}]},
            "claude": {SONNET: [lying, mul("*", "  # s")]}})
        self.assertEqual(code, 0, err)
        st = self.state_of(doc["task"])
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]], [("G-x", "tried_wrong"), ("S", None)],
                         "the failed R1 rejects the G-x attempt; the S attempt passes its review")
        self.assertEqual(st["rungs"][0]["signals"]["C"], False)
        reviews = [r for r in self.records("reviewer") if r["rung"] == "S"]
        self.assertEqual(reviews[0]["signals"]["C"], "fail")
        self.assertIn(TASK_TEXT, self.prompts(SONNET)[0], "the review prompt holds the task text")

    def test_two_reviewer_crashes_at_low_risk_defer_the_task(self):
        code, doc, err = self.run_medium({
            "cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("*")]},
            "claude": {SONNET: [CRASH, CRASH, mul("*", "  # s")]}})
        self.assertEqual(code, 75, err)
        st = self.state_of(doc["task"])
        self.assertEqual(st["outcome"], "deferred")
        self.assertIn("reviewer unavailable", st["reason"])
        self.assertEqual(self.models(), [GH, GX, SONNET, SONNET], "no implementer rung after the crashes")
        self.assertEqual([r["rung"] for r in st["rungs"]], ["G-x"])
        self.assertTrue(st["patch"])
        self.assertEqual(self.only_task()["risk"], "low")


if __name__ == "__main__":
    unittest.main()
