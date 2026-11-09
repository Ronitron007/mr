"""gate.review_contract: mr derives signal C from the reviewer's items and its overall (issue #1 findings 3, 8)."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from mr import cli, isolation, review, state
from tests import gitfix
from tests.test_advisor_loop import OPUS_USAGE, SF_TEXT, AdvisorLoopCase, block_result
from tests.test_aj10_defer_mr import ALLOW, DeferCase
from tests.test_aj5_run_mr import LOGIN, PATH_AUTH, AJ5Loop
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


class TestOverallFailFails(ReviewCase):
    """r9.overall_fail_ignored: an overall fail fails the review, also when every item passes."""

    def run_reqs(self, replies, reqs=R1):
        self.setup({"claude": {SONNET: [{"result": t} for t in replies]}})
        return review.review("diff", reqs, None, "grok-4.7-high", "medium", cfg=self.cfg,
                             tree=self.repo, task_id="t1", attempt=1, task_text=TASK_TEXT)

    def test_all_items_pass_with_overall_fail_is_fail(self):
        sig = self.run_reqs([mr_review([item("R1", "pass")], "fail")])
        self.assertEqual((sig.verdict, sig.ok, sig.attempts), ("fail", False, 1))

    def test_plan_less_task_pass_with_overall_fail_is_fail(self):
        sig = self.run_reqs([mr_review([item("TASK", "pass")], "fail")], reqs=None)
        self.assertEqual((sig.verdict, sig.ok), ("fail", False))

    def test_mr_review_exits_1_for_overall_fail(self):
        self.setup({"claude": {SONNET: [{"result": mr_review([item("TASK", "pass")], "fail")}]}})
        cfgpath = os.path.join(self.dir, "routes.yaml")
        with open(cfgpath, "w") as fh:
            fh.write(f"paths:\n  claude: {gitfix.FAKE_CLAUDE}\n  check: {gitfix.FAKE_CHECK}\n"
                     f"  cursor_agent: {gitfix.FAKE_CURSOR}\n  home: {self.home}\n")
        with open(os.path.join(self.repo, "new.txt"), "w") as fh:
            fh.write("hello\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--config", cfgpath, "--json", "review", "--repo", self.repo])
        self.assertEqual(code, 1, out.getvalue())
        self.assertEqual(json.loads(out.getvalue().strip())["verdict"], "fail")


class TestRunOverallFail(LoopCase):
    def test_overall_fail_with_all_items_pass_is_not_accepted(self):
        self.set_scenario({
            "cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("*"), {"result": REVIEW_PASS}]},
            "claude": {SONNET: [{"result": mr_review([item("R1", "pass")], "fail")}, mul("*", "  # s")]}})
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "medium", text=TASK_TEXT)
        self.assertEqual(code, 0, err)
        st = self.state_of(doc["task"])
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]], [("G-x", "tried_wrong"), ("S", None)],
                         "the overall fail rejects the G-x attempt")


class TestAdvisorKeepsUnavailable(AdvisorLoopCase):
    """r9.adv_overwrites_unavailable: the advisor outcome does not replace a reviewer-unavailable stop."""

    def run_critical(self, impl_result):
        self.set_scenario({
            "cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [{"result": "junk"}, {"result": "junk again"}]},
            "claude": {SONNET: [dict(mul("*"), advisor=dict(OPUS_USAGE), result=impl_result)]}})
        with mock.patch.object(isolation, "decide_isolation", return_value="W7"):
            return self.run_json("--type", "bugfix", "--difficulty", "critical", "--rung", "S+", text=SF_TEXT)

    def test_advisor_block_and_reviewer_junk_twice_exit_75(self):
        code, doc, err = self.run_critical(block_result())
        self.assertEqual(code, 75, err)
        self.assertIn("reviewer unavailable", doc["reason"])
        self.assertNotIn("C reviewed", doc["reason"])

    def test_advisor_block_pushback_and_reviewer_junk_twice_exit_75(self):
        relay = "MR_ADVISOR " + json.dumps({"slot": "early", "verdict": "block", "risks": [
            {"what": "late event", "where": "mul.py:1", "severity": "high", "test": "x"}],
            "missing_test": None, "confidence": 0.9, "approach": "change", "relayed": "verbatim"})
        pushback = "stopped\n" + relay + "\nMR_RESULT " + json.dumps({
            "item": None, "status": "pushback", "requirements": [], "deviations": [],
            "pushback": {"type": "advisor_block", "evidence": "advisor says no"}, "confidence": 0.5})
        code, doc, err = self.run_critical(pushback)
        self.assertEqual(code, 75, err)
        self.assertIn("reviewer unavailable", doc["reason"])
        self.assertNotEqual(self.state_of(doc["task"])["rungs"][-1]["class"], "spec_suspect")


class ResumeCase(DeferCase):
    def resume(self, task):
        n = len(self.calls())
        code, out, text = self.cli("--json", "resume", task)
        return code, (json.loads(out) if out.strip().startswith("{") else {}), text, self.models()[n:]

    def queued(self):
        return state.read_queue(self.home)


class TestReviewUnavailableResumes(ResumeCase):
    """r9.unavailable_not_resumable: a reviewer-unavailable stop parks the task; mr resume runs only the review."""

    def defer_review(self, reviews):
        self.set_scenario({"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("*")]},
                           "claude": {SONNET: reviews}})
        code, doc, text = self.go(*("--type", "bugfix", "--difficulty", "medium"))
        self.assertEqual(code, 75, text)
        self.assertIn("reviewer unavailable", doc["reason"])
        return doc["task"]

    def test_unavailable_review_resumes_with_only_a_reviewer_call(self):
        tid = self.defer_review([CRASH, CRASH, {"result": REVIEW_PASS}])
        st = self.state_of(tid)
        self.assertEqual(st["next"], "check_judge")
        self.assertEqual([(e["task"], e["check"]) for e in self.queued()], [(tid, "review")])
        reviewer_prompt = self.prompts(SONNET)[0]
        code, doc, text, called = self.resume(tid)
        self.assertEqual(code, 0, text)
        self.assertEqual(doc.get("outcome"), "accepted", text)
        self.assertEqual(called, [SONNET], "mr resume runs no planner and no implementer, only the review")
        self.assertEqual(self.prompts(SONNET)[-1], reviewer_prompt, "the same reviewer gets the same requirements")
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.state_of(tid)["next"], "done")

    def test_second_unavailable_review_defers_again_with_one_queue_line(self):
        tid = self.defer_review([CRASH, CRASH, CRASH, CRASH])
        code, doc, text, called = self.resume(tid)
        self.assertEqual(code, 75, text)
        self.assertEqual(called, [SONNET, SONNET])
        self.assertEqual([e["task"] for e in self.queued()], [tid], "no duplicate queue line")
        self.assertEqual(self.state_of(tid)["next"], "check_judge")


class TestDeniedStrongReviewResumes(ResumeCase, AJ5Loop):
    repo_files = {**LoopCase.repo_files, **LOGIN}

    def test_guard_denied_required_review_resumes_with_only_a_reviewer_call(self):
        self.fresh({"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("*")]},
                    "claude": {SONNET: [{"result": REVIEW_PASS}]}}, pct=84.0)
        self.env["MR_HOME"] = self.home
        code, out, err = self.mr("--type", "bugfix", "--difficulty", "hard", text=PATH_AUTH)
        self.assertEqual(code, 75, out + err)
        doc = json.loads(out)
        tid = doc["task"]
        self.assertEqual([(e["task"], e["check"]) for e in self.queued()], [(tid, "review")], doc["reason"])
        self.guard(ALLOW)
        code, doc, text, called = self.resume(tid)
        self.assertEqual(code, 0, text)
        self.assertEqual(called, [SONNET], "mr resume runs only the required review")
        self.assertEqual(self.queued(), [])


class TestIdNormalized(ReviewCase):
    """r9.reask_no_feedback: a requirement id in another case or with one trailing dot or colon still counts."""

    def test_lowercase_and_trailing_dot_ids_are_accepted(self):
        reqs = R1 + [{"id": "R2", "text": "keeps order"}]
        for ids in (["r1", "R2"], ["R1.", "r2:"]):
            with self.subTest(ids=ids):
                self.setup({"claude": {SONNET: [{"result": mr_review([item(i, "pass") for i in ids])}]}})
                sig = review.review("diff", reqs, None, "grok-4.7-high", "medium", cfg=self.cfg,
                                    tree=self.repo, task_id="t1", attempt=1, task_text=TASK_TEXT)
                self.assertEqual((sig.verdict, sig.ok, sig.attempts), ("pass", True, 1))


if __name__ == "__main__":
    unittest.main()
