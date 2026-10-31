"""No hourly Claude cap: hour_max defaults to null, five_hour_max 85 stays."""

import os
import tempfile
import unittest
from unittest import mock

from mr import advisor, config, guard
from mr.defaults import defaults

SAMPLE = {"unit": "points", "five_hour_pct": 30.0, "sample_age_s": 30}


class NoHourCap(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        patcher = mock.patch.dict(os.environ, {"MR_TEST_ALLOW_FAKE_PATHS": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.home = td.name

    def cfg(self, limits=None):
        over = {"paths": {"check": "/nonexistent/fake-check", "home": self.home, "claude": "/nonexistent/fake-claude"}}
        if limits is not None:
            over["claude_limits"] = limits
        return config.from_dict(config._deep_merge(defaults(), over))

    def check(self, cfg, rung="S", **sample):
        data = {k: v for k, v in {**SAMPLE, **sample}.items() if v is not None}
        with mock.patch.object(guard, "run_check", return_value=data):
            return guard.check(rung, cfg, 1793000000.0)

    def test_default_has_no_hour_cap(self):
        lim = self.cfg().claude_limits
        self.assertEqual((lim.hour_max, lim.five_hour_max), (None, 85))

    def test_guard_ignores_hour_but_keeps_five_hour(self):
        cfg = self.cfg()
        res = self.check(cfg, hour_usage=99)
        self.assertEqual((res.allow, res.reason, res.hour), (True, "ok", 99))
        res = self.check(cfg, hour_usage=50, five_hour_pct=82.0)
        self.assertEqual((res.allow, res.reason), (False, "five_hour"))
        res = self.check(cfg)  # the new check.sh shape: no hour_usage, exceeded or limit
        self.assertEqual((res.allow, res.reason, res.hour), (True, "ok", None))

    def test_judge_yield_ignores_hour_when_unset(self):
        cfg = self.cfg()
        self.assertFalse(guard.judge_should_yield(cfg, 99, 0))
        self.assertTrue(guard.judge_should_yield(cfg, 0, 85))

    def test_advisor_fits_ignores_hour_when_unset(self):
        self.assertTrue(advisor._fits({"hour": 99, "reserved": 12, "five_hour_pct": 30}, 12))
        self.assertFalse(advisor._fits({"hour": 0, "reserved": 0, "five_hour_pct": 74}, 12))

    def test_explicit_hour_max_still_denies(self):
        cfg = self.cfg({"hour_max": 30, "five_hour_max": 85})
        res = self.check(cfg, hour_usage=28)
        self.assertEqual((res.allow, res.reason), (False, "budget"))  # 28 + 0 + 4 > 30
        self.assertTrue(self.check(cfg, hour_usage=26).allow)
        self.assertTrue(guard.judge_should_yield(cfg, 30, 0))
        self.assertTrue(self.check(self.cfg(), hour_usage=28).allow)

    def test_bad_hour_max_rejected(self):
        for bad in (0, -1, "x", True):
            with self.subTest(hour_max=bad), self.assertRaises(config.ConfigError):
                self.cfg({"hour_max": bad, "five_hour_max": 85})


if __name__ == "__main__":
    unittest.main()
