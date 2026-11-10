"""gate.caps: caps.claude_rungs_per_task and caps.opus_rungs_per_task bind every implementer launch, and
`mr route` shows the planner, reviewer and check judge rungs beside the ladder."""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from mr import cli, config, loop
from mr.defaults import defaults
from tests import gitfix
from tests.test_loop import _rows, _state
from tests.test_loop_run import GH, GL, GX, OPUS, PLAN, README, REVIEW_PASS, SONNET, LoopCase, mul


def _cfg(**caps):
    return config.from_dict(config._deep_merge(defaults(), {"caps": caps}))


def _after_gx(ladder=("G-x", "S", "O")):
    return loop.LoopState(ladder, "hard", ("G-x",), "G-x", "tried_wrong", 1, 0, 0, 0, (), True, 0)


def _caps_kw(cfg):
    return {"caps": cfg.caps.attempts, "claude_cap": cfg.caps.claude_rungs_per_task,
            "opus_cap": cfg.caps.opus_rungs_per_task}


class TestNextStepCaps(unittest.TestCase):
    def test_zero_caps_after_gx_is_not_s(self):  # Codex repro zero-claude-cap-ignored
        st = loop.next_step(_after_gx(), **_caps_kw(_cfg(claude_rungs_per_task=0, opus_rungs_per_task=0)))
        self.assertEqual((st.action, st.rung, st.exit), ("exit", None, 1), st)

    def test_zero_opus_cap_skips_o_only(self):
        st = loop.next_step(_after_gx(("G-x", "O", "S")), **_caps_kw(_cfg(opus_rungs_per_task=0)))
        self.assertEqual((st.action, st.rung), ("rung", "S"), st)

    def test_default_caps_still_allow_s(self):
        st = loop.next_step(_after_gx(), **_caps_kw(_cfg()))
        self.assertEqual((st.action, st.rung), ("rung", "S"), st)

    def test_fixture_table_with_configured_default_caps(self):  # AC-ESC-1 through the config values
        kw = _caps_kw(_cfg())
        for row in _rows():
            with self.subTest(row["id"]):
                st = loop.next_step(_state(row), **kw)
                got = {"action": st.action, "rung": st.rung, "exit": st.exit,
                       "reset": st.reset, "effort_up": st.effort_up}
                self.assertEqual(got, row["expect"])

    def test_first_rung(self):
        zero = _cfg(claude_rungs_per_task=0, opus_rungs_per_task=0).caps
        self.assertEqual(loop.first_rung(("S", "G-x", "O"), zero), "G-x")
        self.assertIsNone(loop.first_rung(("S+", "O+"), zero))
        self.assertEqual(loop.first_rung(("S", "G-x"), _cfg().caps), "S")


class CapsCase(LoopCase):
    def set_caps(self, claude, opus):
        with open(self.cfgpath, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["caps"] = {"claude_rungs_per_task": claude, "opus_rungs_per_task": opus}
        with open(self.cfgpath, "w", encoding="utf-8") as fh:
            json.dump(raw, fh)


class TestRunCaps(CapsCase):
    def test_zero_caps_stop_after_gx_fails(self):
        self.set_caps(0, 0)
        self.set_scenario({"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("+")]},
                           "claude": {SONNET: [mul("*", "  # fixed")]}})
        code, _doc, err = self.run_json("--type", "bugfix", "--difficulty", "medium")
        self.assertEqual(self.models(), [GH, GX])
        self.assertEqual(code, 1, err)

    def test_default_caps_still_escalate_to_s(self):
        self.set_scenario({"cursor": {GH: [{"result": json.dumps(PLAN)}], GX: [mul("+"), {"result": REVIEW_PASS}]},
                           "claude": {SONNET: [mul("*", "  # fixed")]}})
        code, _doc, err = self.run_json("--type", "bugfix", "--difficulty", "medium")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.models(), [GH, GX, SONNET, GX])

    def test_first_launch_skips_a_claude_rung_the_caps_forbid(self):
        self.set_caps(0, 0)
        self.set_scenario({"cursor": {GL: [{"files": README}]}, "claude": {SONNET: [{"files": README}]}})
        code, _doc, err = self.run_json("--type", "docs", "--difficulty", "trivial", "--prefer", "S")
        self.assertEqual(self.models(), [GL])
        self.assertEqual(code, 0, err)

    def test_first_launch_with_no_rung_in_caps_makes_no_model_call(self):
        self.set_caps(0, 0)
        self.set_scenario({"cursor": {GX: [{"result": REVIEW_PASS}]},
                           "claude": {OPUS: [{"result": json.dumps(PLAN)}], SONNET: [mul("*", "  # fixed")]}})
        code, _doc, err = self.run_json("--type", "bugfix", "--difficulty", "critical")
        self.assertEqual(self.models(), [])
        self.assertEqual(code, 1, err)
        self.assertIn("caps", err)


class TestRouteRoles(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="mr-gate-caps-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = os.path.join(self.root, "repo")
        gitfix.make_repo(self.repo, {"calc.py": "def old():\n    return 1\n"}, branch="feature/x")
        self.cfgpath = os.path.join(self.root, "routes.json")
        with open(self.cfgpath, "w", encoding="utf-8") as fh:
            json.dump({"paths": {"home": os.path.join(self.root, "home")}}, fh)

    def route(self, *extra, json_out=True):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--config", self.cfgpath, *(["--json"] if json_out else []), "route", "add new() to calc.py",
                "--repo", self.repo, "--no-models", *extra]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        self.assertEqual(code, 0, err.getvalue())
        return json.loads(out.getvalue()) if json_out else out.getvalue()

    def judges(self, rung):
        return [{"check": "plan", "rung": rung, "when": "the plan is still invalid after a retry"},
                {"check": "scope", "rung": "S+", "when": "signal D has findings after H and A pass"}]

    def test_hard_code_task_lists_planner_reviewer_and_judges(self):
        doc = self.route("--type", "code", "--difficulty", "hard")
        self.assertEqual(doc["decision"]["ladder"], ["G-x", "S+", "O"])
        self.assertEqual(doc["roles"], {"planner": "O", "reviewer": "S", "judges": self.judges("S+")})

    def test_free_budget_still_shows_the_claude_planner_and_reviewer(self):  # Codex repro free-budget-claude-calls
        doc = self.route("--type", "code", "--difficulty", "hard", "--budget", "free")
        self.assertEqual(doc["decision"]["ladder"], ["G-x"])
        self.assertEqual(doc["roles"], {"planner": "O", "reviewer": "S", "judges": self.judges("S+")})

    def test_trivial_docs_task_has_no_planner_or_reviewer(self):
        roles = self.route("--type", "docs", "--difficulty", "trivial")["roles"]
        self.assertEqual((roles["planner"], roles["reviewer"]), (None, None))
        self.assertEqual([j["check"] for j in roles["judges"]], ["scope"])

    def test_high_risk_path_shows_the_strong_planner_mr_run_adds(self):  # pipeline.start require_strong
        repo = os.path.join(self.root, "deps-repo")
        gitfix.make_repo(repo, {"package.json": "{}\n"}, branch="feature/x")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--config", self.cfgpath, "--json", "route", "bump the version in package.json",
                             "--repo", repo, "--no-models", "--type", "bugfix", "--difficulty", "easy"])
        self.assertEqual(code, 0, err.getvalue())
        doc = json.loads(out.getvalue())
        self.assertIsNone(doc["decision"]["planner"])
        self.assertEqual((doc["roles"]["planner"], doc["roles"]["reviewer"]), ("S+", "S+"))

    def test_text_output_shows_the_roles(self):
        out = self.route("--type", "code", "--difficulty", "hard", "--budget", "free", json_out=False)
        self.assertIn("ladder: G-x\n", out)
        self.assertIn("planner: O\n", out)
        self.assertIn("reviewer: S\n", out)
        self.assertIn("judges: plan S+ (the plan is still invalid after a retry), "
                      "scope S+ (signal D has findings after H and A pass)", out)


class TestOnceCaps(CapsCase):  # caps.once_rung_bypass
    def test_once_rung_over_the_caps_makes_no_model_call(self):
        for rung, model in (("O", OPUS), ("S", SONNET)):
            with self.subTest(rung):
                self.set_caps(0, 0)
                self.set_scenario({"claude": {model: [{"files": README}]}})
                code, _doc, err = self.run_json("--once", "--rung", rung, "--type", "docs", "--difficulty", "trivial")
                self.assertEqual(self.models(), [])
                self.assertEqual(code, 1, err)
                self.assertIn("no ladder rung fits the caps", err)

    def test_once_rung_within_the_caps_still_runs(self):
        self.set_scenario({"claude": {SONNET: [{"files": README}]}})
        code, _doc, err = self.run_json("--once", "--rung", "S", "--type", "docs", "--difficulty", "trivial")
        self.assertEqual(self.models(), [SONNET])
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
