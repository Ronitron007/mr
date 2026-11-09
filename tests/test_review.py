"""Tests for mr.review and `mr review` (mr-spec §5.9 AC-SIG-2, §7.5a AC-REV-1)."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from mr import cli, config, review
from mr.defaults import defaults
from tests import gitfix

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "p0",
                        "mr_review_fixtures.json")
SONNET = "claude-sonnet-5-5"
COMPOSER = "composer-2.5"
GROK_X = "grok-4.7-xhigh"
CHECK_DEFAULT = {
    "hour_usage": 10, "limit": 40, "unit": "points", "exceeded": False,
    "five_hour_pct": 30.0, "five_hour_resets_at": "2026-10-30T05:00:00Z",
    "seven_day_pct": 12.0, "sample_age_s": 30, "samples_in_hour": 3,
    "has_baseline": True, "checked_at": "2026-10-30T00:00:00Z",
}


def _valid(overall="pass"):
    return ('notes\nMR_REVIEW {"items":[{"id":"R1","verdict":"pass",'
            f'"evidence":"a.py:1"}}],"overall":"{overall}"}}\n')


class ReviewCase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = td.name
        self.home = os.path.join(self.dir, "home")
        self.repo = gitfix.make_repo(os.path.join(self.dir, "repo"))

    def setup(self, scenario, **cfg_paths):
        sp = gitfix.write_scenario(self.dir, scenario)
        for suffix in (".state.json", ".calls.jsonl"):  # fresh counters per scenario
            try:
                os.remove(sp + suffix)
            except FileNotFoundError:
                pass
        gitfix.write_check(self.dir, {"default": dict(CHECK_DEFAULT)})
        self.sp = sp
        patcher = mock.patch.dict(os.environ, gitfix.fake_env(
            sp, os.path.join(self.dir, "check.json")))
        patcher.start()
        self.addCleanup(patcher.stop)
        paths = {"home": self.home, "cursor_agent": gitfix.FAKE_CURSOR,
                 "claude": gitfix.FAKE_CLAUDE, "check": gitfix.FAKE_CHECK}
        paths.update(cfg_paths)
        self.cfg = config.from_dict(config._deep_merge(defaults(), {"paths": paths}))

    def calls(self):
        with open(self.sp + ".calls.jsonl") as fh:
            return [json.loads(line) for line in fh]

    def run_review(self, risk="medium", author="grok-4.7-high"):
        return review.review("diff", None, None, author, risk, cfg=self.cfg,
                             tree=self.repo, task_id="t1", attempt=1)


class TestFamilyMap(unittest.TestCase):
    def test_review_family_map(self):  # AC-SIG-2
        cfg = config.from_dict(defaults())
        for fam in ("grok", "kimi", "composer", "claude"):
            for risk in ("low", "medium", "high"):
                with self.subTest(fam=fam, risk=risk):
                    rung = review.reviewer_rung(fam, risk, cfg)
                    rfam = review.family(cfg.rungs[rung].model)
                    self.assertNotEqual(rfam, fam)
                    self.assertNotEqual({rfam, fam}, {"kimi", "composer"})
                    if fam == "grok":
                        self.assertEqual(rung, "S")  # Sonnet reviews Grok, high risk or not
                    elif fam in ("kimi", "composer"):
                        self.assertEqual(rung, "G-h")
                    else:
                        self.assertEqual(rung, "G-x")  # G-x reviews Claude output
        with self.assertRaises(ValueError):
            review.family("gpt-5")

    def test_claude_denied_fallback_not_anthropic(self):  # hard rule §0.3
        cfg = config.from_dict(defaults())
        self.assertEqual(cfg.review.claude_denied, "C25")
        self.assertEqual(cfg.rungs["C25"].path, "cursor")
        self.assertFalse(config.is_anthropic(cfg.rungs["C25"].model))
        self.assertEqual(review.family(cfg.rungs["C25"].model), "composer")

    def test_no_paid_parameters(self):
        import inspect
        self.assertNotIn("paid_left", inspect.signature(review.reviewer_rung).parameters)
        self.assertNotIn("paid_left", inspect.signature(review.review).parameters)
        self.assertFalse(hasattr(review, "_PAID_KIMI"))


class TestFixturesParse(unittest.TestCase):
    def test_mr_review_fixtures_parse(self):  # AC-REV-1, single rows
        with open(FIXTURES) as fh:
            rows = json.load(fh)
        for row in rows:
            if "text" in row:
                with self.subTest(id=row["id"]):
                    self.assertEqual(review.parse_review(row["text"]), row["expect"])


class TestFixturesRetry(ReviewCase):
    def test_mr_review_fixtures_retry(self):  # AC-REV-1, re-ask pairs
        with open(FIXTURES) as fh:
            rows = [r for r in json.load(fh) if "attempts" in r]
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(id=row["id"]):
                self.setup({"claude": {SONNET: [{"result": t} for t in row["attempts"]]}})
                sig = self.run_review()
                self.assertEqual(sig.verdict, row["expect_overall"])
                self.assertEqual(sig.attempts, 2)


class TestUnclearRisk(ReviewCase):
    def test_review_unclear_risk(self):
        for risk, ok in (("low", True), ("medium", False)):
            with self.subTest(risk=risk):
                self.setup({"claude": {SONNET: [{"result": _valid("unclear")}]}})
                sig = self.run_review(risk=risk)
                self.assertEqual(sig.verdict, "unclear")
                self.assertEqual(sig.ok, ok)
                self.assertEqual(bool(sig.warning), ok)
                self.assertEqual(sig.attempts, 1)


class TestGrokAuthorReviewer(ReviewCase):
    def test_grok_author_reviewed_by_sonnet(self):  # roles: S reviews Grok output
        self.setup({"claude": {SONNET: [{"result": _valid()}]}})
        sig = self.run_review(risk="medium")
        self.assertEqual(sig.reviewer, "S")
        self.assertEqual(sig.verdict, "pass")
        self.assertFalse(sig.denied)
        self.assertEqual([rec["cli"] for rec in self.calls()], ["claude"])

    def test_claude_author_reviewed_by_grok_x(self):  # roles: G-x reviews Claude output
        self.setup({"cursor": {GROK_X: [{"result": _valid()}]}})
        sig = self.run_review(author=SONNET)
        self.assertEqual(sig.reviewer, "G-x")
        self.assertEqual(sig.verdict, "pass")
        self.assertEqual([rec["cli"] for rec in self.calls()], ["cursor"])


class TestGuardDenied(ReviewCase):
    def test_review_guard_denied(self):  # S denied -> review.claude_denied (C25), still cross-family
        self.setup({"claude": {SONNET: [{}]}, "cursor": {COMPOSER: [{"result": _valid()}]}})
        gitfix.write_check(self.dir, {"default": dict(CHECK_DEFAULT, five_hour_pct=84)})
        sig = self.run_review(risk="high")
        self.assertTrue(sig.denied)
        self.assertEqual(sig.reviewer, "C25")
        self.assertEqual(sig.verdict, "pass")
        self.assertTrue(sig.ok)
        self.assertTrue(self.calls())
        for rec in self.calls():
            self.assertEqual(rec["cli"], "cursor")

    def test_denied_fallback_must_stay_cross_family(self):  # AC-SIG-2 beats a misconfigured fallback
        self.setup({"claude": {SONNET: [{}]}, "cursor": {"grok-4.7-high": [{"result": _valid()}]}})
        self.cfg = config.from_dict(config._deep_merge(defaults(), {
            "paths": {"home": self.home, "cursor_agent": gitfix.FAKE_CURSOR,
                      "claude": gitfix.FAKE_CLAUDE, "check": gitfix.FAKE_CHECK},
            }))
        import dataclasses  # config now rejects this; the runtime guard still holds for hand-built configs
        self.cfg = dataclasses.replace(self.cfg, review=dataclasses.replace(self.cfg.review, claude_denied="G-h"))
        gitfix.write_check(self.dir, {"default": dict(CHECK_DEFAULT, five_hour_pct=84)})
        sig = self.run_review(risk="high")  # grok author, S denied, G-h is grok: no review at all
        self.assertTrue(sig.denied)
        self.assertEqual(sig.verdict, "unavailable")
        self.assertFalse(sig.ok)
        self.assertIn("not cross-family", sig.warning)
        self.assertFalse(os.path.exists(self.sp + ".calls.jsonl"))  # nothing ran


class TestReviewCli(ReviewCase):
    def _cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_mr_review_cli(self):
        self.setup({"claude": {SONNET: [{"result": _valid()},
                                        {"result": _valid("fail").replace('"pass"', '"fail"', 1)}]}})
        cfgpath = os.path.join(self.dir, "routes.yaml")
        with open(cfgpath, "w") as fh:
            fh.write("paths:\n"
                     f"  claude: {gitfix.FAKE_CLAUDE}\n"
                     f"  check: {gitfix.FAKE_CHECK}\n"
                     f"  cursor_agent: {gitfix.FAKE_CURSOR}\n"
                     f"  home: {self.home}\n")
        with open(os.path.join(self.repo, "new.txt"), "w") as fh:
            fh.write("hello\n")
        status = gitfix.git(self.repo, "status", "--porcelain").stdout
        argv = ["--config", cfgpath, "--json", "review", "--repo", self.repo]
        code, out = self._cli(argv)
        self.assertEqual(code, 0, out)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 1)
        doc = json.loads(lines[0])
        self.assertEqual(doc["verdict"], "pass")
        self.assertTrue(doc["ok"])
        self.assertEqual(doc["reviewer"], "S")
        code, out = self._cli(argv)
        self.assertEqual(code, 1, out)
        self.assertEqual(json.loads(out.strip())["verdict"], "fail")
        self.assertEqual(gitfix.git(self.repo, "status", "--porcelain").stdout, status)


if __name__ == "__main__":
    unittest.main()
