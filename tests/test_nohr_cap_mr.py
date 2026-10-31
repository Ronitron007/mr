"""NOHR: no hourly Claude cap in mr. Locked planner tests.

Notes for the implementer (existing tests to update, all inside scope):
- tests/test_config.py: default claude_limits is {hour_max: None, five_hour_max: 85}; the 'keeping default'
  legacy cases now keep hour_max None.
- tests/test_guard.py: test_boundaries and test_shared_claude_limits pin the hourly rule with an explicit
  claude_limits.hour_max 30.
- tests/test_advisor.py test_gates and the hour-based rows of tests/fixtures/advisor/decide_table.json
  (V31, V34, V49) pass hour_max 30 in the guard input.
- Guard-deny tests that used hour_usage 39 (test_e2e, test_loop_run, test_planner, test_review, test_run_once)
  deny through the 5-hour ceiling instead (e.g. five_hour_pct 84); test_advisor_loop G1 uses five_hour_pct 74.
"""

import os
import tempfile
import types
import unittest
from unittest import mock

from mr import advisor, config, guard, loop
from mr.defaults import defaults

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOW = 1793000000.0
SAMPLE = {
    'unit': 'points',
    'five_hour_pct': 30.0,
    'five_hour_resets_at': '2026-10-30T05:00:00Z',
    'seven_day_pct': 12.0,
    'sample_age_s': 30,
    'samples_in_hour': 3,
    'has_baseline': True,
    'checked_at': '2026-10-30T00:00:00Z',
}


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
        return fh.read()


class NoHourCapCase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.home = td.name
        patcher = mock.patch.dict(os.environ, {'MR_TEST_ALLOW_FAKE_PATHS': '1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def raw(self, limits=None):
        over = {'paths': {'check': '/nonexistent/fake-check', 'home': self.home,
                          'claude': '/nonexistent/fake-claude'}}
        if limits is not None:
            over['claude_limits'] = limits
        return config._deep_merge(defaults(), over)

    def cfg(self, limits=None, raw=None):
        try:
            return config.from_dict(raw if raw is not None else self.raw(limits))
        except Exception as exc:  # the old schema rejected a null or absent hour_max
            self.fail(f'config rejected: {exc!r}')

    def check(self, cfg, rung, **sample):
        data = {**SAMPLE, **sample}
        data = {k: v for k, v in data.items() if v is not None}
        with mock.patch.object(guard, 'run_check', return_value=data):
            return guard.check(rung, cfg, NOW)


class TestDefaults(NoHourCapCase):
    def test_default_hour_max_is_null(self):
        raw = defaults()
        self.assertIsNone(raw['claude_limits'].get('hour_max'))
        self.assertEqual(raw['claude_limits']['five_hour_max'], 85)
        cfg = self.cfg()
        self.assertIsNone(cfg.claude_limits.hour_max)
        self.assertEqual(cfg.claude_limits.five_hour_max, 85)

    def test_config_accepts_null_absent_or_positive_hour_max(self):
        self.assertIsNone(self.cfg({'hour_max': None, 'five_hour_max': 85}).claude_limits.hour_max)
        raw = self.raw()
        raw['claude_limits'].pop('hour_max', None)
        self.assertIsNone(self.cfg(raw=raw).claude_limits.hour_max)
        self.assertEqual(self.cfg({'hour_max': 30, 'five_hour_max': 85}).claude_limits.hour_max, 30)
        for bad in (0, -1, 'x', True):
            with self.subTest(hour_max=bad):
                with self.assertRaises(config.ConfigError):
                    config.from_dict(self.raw({'hour_max': bad, 'five_hour_max': 85}))
        with self.assertRaises(config.ConfigError):
            config.from_dict(self.raw({'hour_max': None, 'five_hour_max': 101}))


class TestGuard(NoHourCapCase):
    def test_guard_allows_huge_hour_usage_without_cap(self):
        cfg = self.cfg()
        res = self.check(cfg, 'S', hour_usage=99, five_hour_pct=30.0)
        self.assertEqual((res.allow, res.reason), (True, 'ok'), res)
        self.assertEqual(res.hour, 99)
        res = self.check(cfg, 'S+', hour_usage=99, exceeded=False, limit=40, five_hour_pct=79.0)
        self.assertEqual((res.allow, res.reason), (True, 'ok'), res)  # 79 + 6 = 85
        res = self.check(cfg, 'S', hour_usage=99, exceeded=True, five_hour_pct=30.0)
        self.assertEqual((res.allow, res.reason), (False, 'over_limit'), res)

    def test_guard_five_hour_ceiling_still_denies(self):
        cfg = self.cfg()
        res = self.check(cfg, 'S', hour_usage=50, five_hour_pct=81.0)
        self.assertEqual((res.allow, res.reason), (True, 'ok'), res)  # 81 + 4 = 85
        res = self.check(cfg, 'S', hour_usage=50, five_hour_pct=82.0)
        self.assertEqual((res.allow, res.reason), (False, 'five_hour'), res)  # 82 + 4 = 86 > 85
        res = self.check(cfg, 'S', hour_usage=0, five_hour_pct=None)
        self.assertEqual((res.allow, res.reason), (False, 'no 5h baseline'), res)

    def test_guard_sample_without_hour_fields_allows(self):
        cfg = self.cfg()
        res = self.check(cfg, 'S')  # no exceeded, limit or hour_usage: the new check.sh shape
        self.assertEqual((res.allow, res.reason), (True, 'ok'), res)
        self.assertIsNone(res.hour)
        self.assertEqual(res.five_hour, 30.0)
        res = self.check(cfg, 'S', hour_usage='junk')
        self.assertEqual((res.allow, res.reason), (True, 'ok'), res)
        self.assertIsNone(res.hour)

    def test_explicit_hour_max_still_denies(self):
        res = self.check(self.cfg(), 'S', hour_usage=28)
        self.assertEqual((res.allow, res.reason), (True, 'ok'), res)  # no hourly cap by default
        cfg = self.cfg({'hour_max': 30, 'five_hour_max': 85})
        res = self.check(cfg, 'S', hour_usage=28)
        self.assertEqual((res.allow, res.reason), (False, 'budget'), res)  # 28 + 0 + 4 = 32 > 30
        res = self.check(cfg, 'S', hour_usage=26)
        self.assertEqual((res.allow, res.reason), (True, 'ok'), res)  # 26 + 0 + 4 = 30
        res = self.check(cfg, 'S', hour_usage=26, five_hour_pct=82.0)
        self.assertEqual((res.allow, res.reason), (False, 'five_hour'), res)

    def test_judge_yield_ignores_hour_when_unset(self):
        cfg = self.cfg()
        self.assertFalse(guard.judge_should_yield(cfg, 99, 0))
        self.assertFalse(guard.judge_should_yield(cfg, 99, 84))
        self.assertTrue(guard.judge_should_yield(cfg, 0, 85))
        cfg30 = self.cfg({'hour_max': 30, 'five_hour_max': 85})
        self.assertFalse(guard.judge_should_yield(cfg30, 29, 0))
        self.assertTrue(guard.judge_should_yield(cfg30, 30, 0))
        self.assertTrue(guard.judge_should_yield(cfg30, 0, 85))


class TestAdvisor(NoHourCapCase):
    def test_advisor_fits_ignores_hour_when_unset(self):
        self.assertIsNone(advisor.GUARD.get('hour_max'))
        self.assertEqual(advisor.GUARD['five_hour_max'], 85)
        self.assertTrue(advisor._fits({'hour': 99, 'reserved': 12, 'five_hour_pct': 30}, 12))
        self.assertTrue(advisor._fits({'hour': 99, 'reserved': 0, 'five_hour_pct': 73, 'hour_max': None}, 12))
        self.assertTrue(advisor._fits({'hour': None, 'reserved': 0, 'five_hour_pct': 30}, 12))
        self.assertFalse(advisor._fits({'hour': 0, 'reserved': 0, 'five_hour_pct': 74}, 12))  # 74 + 12 > 85
        explicit = {'reserved': 0, 'five_hour_pct': 30, 'hour_max': 30}
        self.assertTrue(advisor._fits({**explicit, 'hour': 18}, 12))  # 18 + 12 = 30
        self.assertFalse(advisor._fits({**explicit, 'hour': 19}, 12))  # 19 + 12 = 31 > 30

    def _guard_in(self, cfg, g):
        seen = []

        def decide(inp, cfg_adv):
            seen.append(inp['guard'])
            return advisor.AdvisorDecision(False, (), 'N7', ('N7: test',), 0, 0.0, 'ok', 'S+')

        c = types.SimpleNamespace(
            args=types.SimpleNamespace(advisor=None, budget=None, advisor_consults=None, advisor_slot=None),
            cfg=cfg,
            task=types.SimpleNamespace(id='t-nohr', text='Fix the event stream race in mul.py.',
                                       repo=self.home, files_named=[]),
            d=types.SimpleNamespace(route='cascade', type='bugfix', difficulty='hard', risk='medium',
                                    ambiguity='low', load_bearing=False),
            checklist=None, rungs=[], log=[], seq=0)
        with mock.patch.object(loop.guard, 'check', return_value=g):
            with mock.patch.object(loop.advisor, 'decide', side_effect=decide):
                with mock.patch.object(loop, '_adv_counts', return_value=(0, 0)):
                    self.assertEqual(loop.advisor_rung(c, 'S+'), 'S+')
        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_loop_advisor_guard_input_has_no_hour_cap(self):
        cfg = self.cfg()
        gin = self._guard_in(cfg, guard.GuardResult(True, 'ok', 99.0, 0.0, 12.0, 30.0, None, False, None))
        self.assertIsNone(gin.get('hour_max'))
        self.assertTrue(advisor._fits(gin, 12))
        gin = self._guard_in(cfg, guard.GuardResult(True, 'ok', None, 0.0, 12.0, 30.0, None, False, None))
        self.assertTrue(advisor._fits(gin, 12))
        gin = self._guard_in(cfg, guard.GuardResult(False, 'guard error', None, 0.0, 12.0, None, 'x', False, None))
        self.assertFalse(advisor._fits(gin, 12))  # fail safe: no 5h reading counts as the ceiling
        cfg30 = self.cfg({'hour_max': 30, 'five_hour_max': 85})
        gin = self._guard_in(cfg30, guard.GuardResult(False, 'budget', 24.0, 0.0, 12.0, 30.0, 'x', False, None))
        self.assertEqual(gin.get('hour_max'), 30)
        self.assertFalse(advisor._fits(gin, 12))  # 24 + 12 = 36 > 30
        self.assertTrue(advisor._fits(gin, 6))  # 24 + 6 = 30


class TestDefaultsText(unittest.TestCase):
    def test_defaults_text(self):
        line = 'claude_limits: {hour_max: null, five_hour_max: 85}'
        self.assertIn(line, _read('mr/defaults.py'))


if __name__ == '__main__':
    unittest.main()
