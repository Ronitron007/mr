"""`mr run` without --once: loop.run_task against the fakes (mr-spec §14.3 S1, S2, S3, S6; AC-LOOP-1)."""
import json
import os
import unittest

from tests import gitfix
from tests.gitfix import git
from tests.test_run_once import CHECK_DEFAULT, RunCase

SANITY = ("import unittest\n\n\nclass TestSanity(unittest.TestCase):\n"
          "    def test_ok(self):\n        self.assertTrue(True)\n")
RED = ("import mul\nimport unittest\n\n\nclass TestMul(unittest.TestCase):\n"
       "    def test_mul_basic(self):\n        self.assertEqual(mul.mul(2, 3), 6)\n")
TEST_PATH = "tests/test_mul_mr.py"
PLAN = {
    "goal": "Add mul.",
    "scope": {"modify": ["README.md"], "create": ["mul.py", TEST_PATH], "forbidden": ["package.json"]},
    "requirements": [{"id": "R1", "text": "mul SHALL multiply",
                     "examples": [{"given": "2, 3", "when": "mul", "then": "6"}]}],
    "acceptance": [{"id": "A1", "covers": ["R1"], "file": TEST_PATH, "tests": ["test_mul_basic"]}],
    "est_loc": 10,
    "files_map": {"R1": ["mul.py"]},
    "tests": [{"path": TEST_PATH, "content": RED}],
}
REVIEW_PASS = ("looks right\nMR_REVIEW " + json.dumps(
    {"items": [{"id": "R1", "verdict": "pass", "evidence": "mul.py:2"}], "overall": "pass"}))
README = {"README.md": "# test repo\nfixed typo\n"}
GL, GM, GH, GX = ("grok-4.7-low", "grok-4.7-medium", "grok-4.7-high",
                  "grok-4.7-xhigh")
SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"


def mul(op, note=""):
    # the note changes the size: same-size rewrites within one second would reuse a stale .pyc
    return {"files": {"mul.py": f"def mul(a, b):\n    return a {op} b{note}\n"}}


class LoopCase(RunCase):
    repo_files = {"tests/test_sanity.py": SANITY, ".gitignore": "build/\nnode_modules/\n__pycache__/\n"}
    hygiene_cmds = ["python3 -m unittest tests.test_sanity"]

    def write_cfg(self):
        raw = {"paths": {"home": self.home, "cursor_agent": gitfix.FAKE_CURSOR,
                         "claude": gitfix.FAKE_CLAUDE, "check": gitfix.FAKE_CHECK},
               "hygiene": {"commands": {"repo": self.hygiene_cmds}},
               "acceptance": {"runners": {"repo": "unittest"}}}
        with open(self.cfgpath, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)

    def argv(self, *extra, text="fix typo in README.md"):
        return ["--config", self.cfgpath, "--json", "run", text, "--repo", self.repo, *extra]

    def run_json(self, *extra, **kw):
        code, out, err = self.mr(*extra, **kw)
        return code, (json.loads(out) if out.strip() else {}), err

    def models(self):
        return [c["model"] for c in self.calls()]

    def prompts(self, model):
        return [c["prompt"] for c in self.calls() if c["model"] == model]



class TestCascade(LoopCase):
    def test_s1_trivial_docs_pass(self):
        self.cursor({"files": README}, model=GL)
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.models(), [GL])
        st = self.state_of(doc["task"])
        self.assertEqual((st["isolation"], st["next"], st["outcome"]), ("W7", "done", "accepted"))
        self.assertEqual([r["rung"] for r in st["rungs"]], ["G-l"])
        self.assertIsNone(st["checklist"])
        self.assertIn("(none — test files in SCOPE may be edited)", self.prompts(GL)[0])
        self.assertIn("python3 -m unittest tests.test_sanity", self.prompts(GL)[0])
        self.assertNotIn("npx tsc", self.prompts(GL)[0])
        self.assertIn("fixed typo", git(self.repo, "diff", "HEAD", "--", "README.md").stdout)  # W7: uncommitted
        self.assertEqual(os.path.getsize(self.lock_path()), 0)
        self.assert_schema()

    def test_s2_medium_tried_wrong_then_sonnet_passes(self):
        self.set_scenario({
            "cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("+"), {"result": REVIEW_PASS}]},
            "claude": {SONNET: [mul("*", "  # fixed")]}})
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "medium")
        self.assertEqual(code, 0, err)
        # planner G-h, G-x, S, then G-x reviews the Claude author's patch
        self.assertEqual(self.models(), [GH, GX, SONNET, GX])
        st = self.state_of(doc["task"])
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]], [("G-x", "tried_wrong"), ("S", None)])
        self.assertEqual(st["rungs"][0]["signals"]["A"], False)
        self.assertEqual(st["rungs"][1]["signals"], {"H": True, "D": True, "A": True, "C": True})
        self.assertEqual((st["isolation"], st["attempts"], st["used_rungs"]), ("W7", 2, ["G-x", "S"]))
        self.assertEqual(st["checklist"]["goal"], "Add mul.")
        self.assertIn(TEST_PATH, self.prompts(GX)[0])  # locked file listed
        handoff = self.prompts(SONNET)[0]
        self.assertIn("Previous rung: G-x, class tried_wrong", handoff)
        self.assertIn(f"{TEST_PATH}::test_mul_basic", handoff)
        task = self.only_task()
        self.assertEqual(task["attempts"], {"impl": 2, "review": 1, "plan": 1, "classifier": 0})
        self.assertEqual(task["esc"]["tried_wrong"], 1)
        self.assertEqual(task["cost"]["cursor_paid_rungs"], 0)  # paid allowance retired: fixed at 0
        reviewer = self.records("reviewer")
        self.assertEqual([(r["rung"], r["model"]) for r in reviewer], [("G-x", GX)])
        self.assertEqual({"planner", "implementer", "reviewer", "task"} - {r["role"] for r in self.log()}, set())
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "mul.py")))  # W7: left uncommitted
        self.assert_schema()

    def test_s2_c_fail_is_tried_wrong(self):
        fail = "no\nMR_REVIEW " + json.dumps(
            {"items": [{"id": "R1", "verdict": "fail", "evidence": "mul.py:2 wrong"}], "overall": "fail"})
        # G-x passes A; its reviewer (S) fails it once; the S rung then keeps the tree and is reviewed by G-x
        self.set_scenario({
            "cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("*"), {"result": REVIEW_PASS}]},
            "claude": {SONNET: [{"result": fail}, {}]}})
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "medium")
        self.assertEqual(code, 0, err)
        st = self.state_of(doc["task"])
        self.assertEqual([r["class"] for r in st["rungs"]], ["tried_wrong", None])
        self.assertEqual(st["rungs"][0]["signals"]["C"], False)
        self.assertIn("R1: mul.py:2 wrong", self.prompts(SONNET)[1])
        self.assertEqual([r["rung"] for r in self.records("reviewer")], ["S", "G-x"])

    def test_hard_plans_on_opus_via_claude(self):
        self.set_scenario({
            "cursor": {GX: [mul("*")]},
            "claude": {OPUS: [{"result": json.dumps(PLAN)}], SONNET: [{"result": REVIEW_PASS}]}})
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "hard")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.models(), [OPUS, GX, SONNET])  # planner O, G-x, reviewer S
        self.assertEqual([c["cli"] for c in self.calls()], ["claude", "cursor", "claude"])
        planner = self.records("planner")
        self.assertEqual([(r["rung"], r["model"], r["path"]) for r in planner], [("O", OPUS, "max")])
        st = self.state_of(doc["task"])
        self.assertEqual([r["rung"] for r in st["rungs"]], ["G-x"])  # the planner is not a rung
        self.assertEqual((st["attempts"], st["claude_rungs"]), (1, 0))  # reviewer and planner calls are no rungs
        self.assert_schema()

    def test_claude_reviews_do_not_charge_claude_cap_hard(self):
        fail = "no\nMR_REVIEW " + json.dumps(
            {"items": [{"id": "R1", "verdict": "fail", "evidence": "mul.py:2 wrong"}], "overall": "fail"})
        # G-x rejected by the S review, S+ rejected by the G-x review: O must still be reachable
        self.set_scenario({
            "cursor": {GX: [mul("*"), {"result": fail}, {"result": REVIEW_PASS}]},
            "claude": {OPUS: [{"result": json.dumps(PLAN)}, mul("*", "  # o")],
                       SONNET: [{"result": fail}, mul("*", "  # s")]}})
        code, doc, err = self.run_json("--type", "code", "--difficulty", "hard")
        self.assertEqual(code, 0, err)
        st = self.state_of(doc["task"])
        self.assertEqual([r["rung"] for r in st["rungs"]], ["G-x", "S+", "O"])
        self.assertEqual((st["attempts"], st["claude_rungs"]), (3, 2))

    def test_claude_reviews_do_not_charge_claude_cap_medium(self):
        fail = "no\nMR_REVIEW " + json.dumps(
            {"items": [{"id": "R1", "verdict": "fail", "evidence": "mul.py:2 wrong"}], "overall": "fail"})
        # code medium [G-h, G-x, S]: both Grok rungs rejected by the S review, S rung still runs
        self.set_scenario({
            "cursor": {GH: [{"result": json.dumps(PLAN)}, mul("*"), {"result": REVIEW_PASS}],
                       GX: [mul("*", "  # x")]},
            "claude": {SONNET: [{"result": fail}, {"result": fail}, mul("*", "  # s")]}})
        code, doc, err = self.run_json("--type", "code", "--difficulty", "medium")
        self.assertEqual(code, 0, err)
        st = self.state_of(doc["task"])
        self.assertEqual([r["rung"] for r in st["rungs"]], ["G-h", "G-x", "S"])
        self.assertEqual(st["claude_rungs"], 1)

    def test_hard_planner_denied_falls_back_to_g_h(self):
        self.set_scenario({
            "cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("*")]},
            "claude": {OPUS: [{}], SONNET: [{"result": REVIEW_PASS}]}},
            check={"default": dict(CHECK_DEFAULT, five_hour_pct=84)})
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "hard", "--budget", "low")
        self.assertEqual(self.models()[:2], [GH, GX], err)  # O denied by the guard: planner ran on G-h
        planner = self.records("planner")
        self.assertEqual([r["rung"] for r in planner], ["G-h"])
        self.assertIn("planner O denied", planner[0]["signals"]["fallback"][0])
        self.assert_schema()

    def test_s3_skipped_effort_up(self):
        self.set_scenario({"cursor": {GM: [{}], GH: [{"files": README}]}})
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial", "--rung", "G-m")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.models(), [GM, GH])
        st = self.state_of(doc["task"])
        self.assertEqual([(r["rung"], r["class"]) for r in st["rungs"]], [("G-m", "skipped"), ("G-h", None)])
        self.assertEqual(self.only_task()["esc"]["skipped"], 1)

    def test_off_track_resets_before_next_rung(self):
        self.set_scenario({"cursor": {GL: [{"files": {"sub/other.txt": "x\n", **README}}],
                                      GM: [{"files": README}]}})
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "easy")
        self.assertEqual(code, 0, err)
        st = self.state_of(doc["task"])
        self.assertEqual([r["class"] for r in st["rungs"]], ["off_track", None])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "sub", "other.txt")))
        self.assertIn("was reset to the start of the task", self.prompts(GM)[0])
        with open(st["rungs"][1]["patch"], encoding="utf-8") as fh:
            self.assertNotIn("sub/other.txt", fh.read())

    def test_planner_invalid_twice_exit_3(self):
        self.set_scenario({"cursor": {GH: [{"result": "not json"}]}})
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "medium")
        self.assertEqual(code, 3, err)
        self.assertEqual(self.models(), [GH, GH])
        tdir = os.path.join(self.home, "tasks", doc["task"])
        for n in (1, 2):
            self.assertTrue(os.path.isfile(os.path.join(tdir, f"planner-a{n}.txt")))
        self.assertEqual(doc["rungs"], [])
        self.assertEqual(doc["outcome"], "needs_human")
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, "")
        self.assertEqual(os.path.getsize(self.lock_path()), 0)
        self.assertEqual(self.only_task()["exit"], 3)
        self.assert_schema()

    def test_s6_claude_denied_fallback_then_75(self):
        self.set_scenario(
            {"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("+")]},
             "claude": {SONNET: [{}]}},
            check={"default": dict(CHECK_DEFAULT, five_hour_pct=84)})
        code, doc, err = self.run_json("--type", "bugfix", "--difficulty", "medium", "--rung", "S")
        self.assertEqual(code, 75, err)
        # planner, then the fallback G-x; O is denied too and G-x is used, so the chain is empty; Claude never ran
        self.assertEqual(self.models(), [GH, GX])
        self.assertEqual([r["class"] for r in doc["rungs"]], ["guard_deny", "tried_wrong"])
        self.assertEqual(self.state_of(doc["task"])["replans"], 0)
        self.assertEqual(doc["outcome"], "deferred")
        self.assertTrue(doc["frees_hint"])
        with open(doc["patch"], encoding="utf-8") as fh:
            self.assertIn("a + b", fh.read())
        self.assertTrue(os.path.isfile(doc["final_md"]))
        self.assertEqual(os.path.getsize(self.lock_path()), 0)
        self.assert_schema()


class TestW5(LoopCase):
    branch = "main"

    def test_accept_commits_with_trailer(self):
        self.cursor({"files": README}, model=GL)
        code, doc, err = self.run_json("--type", "docs", "--difficulty", "trivial")
        self.assertEqual(code, 0, err)
        self.assertTrue(doc["branch"].startswith("mr/"))
        self.assertIn(f"Mr-Task: {doc['task']}", git(self.repo, "log", "-1", "--format=%B").stdout)
        self.assertEqual(git(self.repo, "status", "--porcelain").stdout, "")


if __name__ == "__main__":
    unittest.main()
