# SHD6 locked tests: judge call budget ledger, judge_recent in the rung guard, breaker and answer cache.
import contextlib
import hashlib
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from mr import config, guard
from mr.defaults import defaults
from tests import gitfix

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HAIKU = 'claude-haiku-5-5'
NL = chr(10)
DAY = 86400
COLS = ['key', 'use', 'backend', 'qset_sha', 'created_at', 'expires_at', 'answers_json', 'usage_json',
        'model_served']
CHECK = {
    'hour_usage': 10,
    'limit': 40,
    'unit': 'points',
    'exceeded': False,
    'five_hour_pct': 30.0,
    'five_hour_resets_at': '2026-10-30T05:00:00Z',
    'seven_day_pct': 12.0,
    'sample_age_s': 30,
    'samples_in_hour': 3,
    'has_baseline': True,
    'checked_at': '2026-10-30T00:00:00Z',
}


def _ts(iso):
    return datetime.fromisoformat(iso).timestamp()


T0 = _ts('2026-10-30T06:00:00+00:00')


class LedgerCase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.home = os.path.realpath(td.name)
        self.check_path = gitfix.write_check(self.home, {'default': dict(CHECK)})
        patcher = mock.patch.dict(os.environ, {'MR_HOME': self.home, 'MR_FAKE_CHECK': self.check_path})
        patcher.start()
        self.addCleanup(patcher.stop)

    def cfg(self, sub='a', budget=None, check=''):
        home = os.path.join(self.home, sub)
        os.makedirs(home, exist_ok=True)
        over = {'paths': {'home': home, 'check': check, 'claude': 'claude'}}
        if budget is not None:
            over['judge'] = {'budget': budget}
        return config.from_dict(config._deep_merge(defaults(), over))

    def mod(self, name):
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as exc:
            self.fail(f'{name} is missing: {exc}')

    def ledger(self, cfg):
        with open(os.path.join(cfg.paths.home, 'state', 'judge-budget.json'), encoding='utf-8') as fh:
            return json.load(fh)


class TestBudget(LedgerCase):
    def test_budget_day_and_task_caps(self):
        budget = self.mod('mr.judge.budget')
        cfg = self.cfg('day', {'calls_per_hour': None, 'calls_per_day': 3, 'max_calls_per_task': 100})
        for i in range(3):
            ok, reason, after = budget.charge(cfg, f't{i}', 'on', now=T0 + i)
            self.assertTrue(ok, f'call {i}')
            self.assertIsNone(reason, f'call {i}')
        self.assertEqual(set(after), {'calls_hour', 'calls_day', 'calls_task'})
        self.assertEqual(after['calls_day'], 3)
        ok, reason, after = budget.charge(cfg, 't9', 'on', now=T0 + 10)
        self.assertEqual((ok, reason), (False, 'budget_denied'))
        self.assertEqual(after['calls_day'], 3, 'a denied call is not counted')
        cfg = self.cfg('task', {'calls_per_hour': None, 'calls_per_day': 150, 'max_calls_per_task': 2})
        self.assertTrue(budget.charge(cfg, 'tx', 'on', now=T0)[0])
        ok, reason, after = budget.charge(cfg, 'tx', 'on', now=T0 + 1)
        self.assertTrue(ok, reason)
        self.assertEqual(after['calls_task'], 2)
        self.assertEqual(budget.charge(cfg, 'tx', 'on', now=T0 + 2)[:2], (False, 'budget_denied'))
        self.assertTrue(budget.charge(cfg, 'ty', 'on', now=T0 + 3)[0], 'another task still has calls')

    def test_budget_hourly_cap_only_when_set(self):
        budget = self.mod('mr.judge.budget')
        cfg = self.cfg('nohour', {'calls_per_hour': None, 'calls_per_day': 1000, 'max_calls_per_task': 1000})
        for i in range(30):
            ok, reason, _ = budget.charge(cfg, 't1', 'on', now=T0 + i)
            self.assertTrue(ok, f'call {i}: no hourly cap when calls_per_hour is null ({reason})')
        cfg = self.cfg('hour2', {'calls_per_hour': 2, 'calls_per_day': 1000, 'max_calls_per_task': 1000})
        self.assertTrue(budget.charge(cfg, 't1', 'on', now=T0)[0])
        self.assertTrue(budget.charge(cfg, 't2', 'on', now=T0 + 60)[0])
        ok, reason, after = budget.charge(cfg, 't3', 'on', now=T0 + 120)
        self.assertEqual((ok, reason), (False, 'budget_denied'))
        self.assertEqual(after['calls_hour'], 2)
        ok, reason, after = budget.charge(cfg, 't4', 'on', now=T0 + 3601)
        self.assertTrue(ok, reason)
        self.assertEqual(after['calls_hour'], 2)

    def test_budget_shadow_stops_at_80_percent(self):
        budget = self.mod('mr.judge.budget')
        cfg = self.cfg('day', {'calls_per_hour': None, 'calls_per_day': 10, 'max_calls_per_task': 1000})
        for i in range(8):
            self.assertTrue(budget.charge(cfg, f't{i}', 'shadow', now=T0 + i)[0], f'shadow call {i}')
        self.assertEqual(budget.charge(cfg, 't8', 'shadow', now=T0 + 8)[:2], (False, 'budget_denied'))
        self.assertTrue(budget.charge(cfg, 't9', 'on', now=T0 + 9)[0])
        self.assertTrue(budget.charge(cfg, 't10', 'on', now=T0 + 10)[0])
        self.assertEqual(budget.charge(cfg, 't11', 'on', now=T0 + 11)[:2], (False, 'budget_denied'))
        cfg = self.cfg('hour', {'calls_per_hour': 5, 'calls_per_day': 1000, 'max_calls_per_task': 1000})
        for i in range(4):
            self.assertTrue(budget.charge(cfg, f't{i}', 'shadow', now=T0 + i)[0], f'hourly shadow call {i}')
        self.assertEqual(budget.charge(cfg, 't4', 'shadow', now=T0 + 4)[:2], (False, 'budget_denied'))
        self.assertTrue(budget.charge(cfg, 't5', 'on', now=T0 + 5)[0])
        cfg = self.cfg('task', {'calls_per_hour': None, 'calls_per_day': 1000, 'max_calls_per_task': 5})
        for i in range(4):
            self.assertTrue(budget.charge(cfg, 'tx', 'shadow', now=T0 + i)[0], f'task shadow call {i}')
        self.assertEqual(budget.charge(cfg, 'tx', 'shadow', now=T0 + 4)[:2], (False, 'budget_denied'))
        self.assertTrue(budget.charge(cfg, 'tx', 'on', now=T0 + 5)[0])

    def test_budget_day_rolls_at_ist_midnight(self):
        budget = self.mod('mr.judge.budget')
        cfg = self.cfg('roll', {'calls_per_hour': None, 'calls_per_day': 2, 'max_calls_per_task': 1000})
        late = _ts('2026-10-30T18:00:00+00:00')  # 23:30 IST
        self.assertTrue(budget.charge(cfg, 't1', 'on', now=late)[0])
        self.assertTrue(budget.charge(cfg, 't2', 'on', now=late + 1)[0])
        denied = budget.charge(cfg, 't3', 'on', now=_ts('2026-10-30T18:20:00+00:00'))  # 23:50 IST
        self.assertEqual(denied[:2], (False, 'budget_denied'))
        ok, reason, after = budget.charge(cfg, 't4', 'on', now=_ts('2026-10-30T18:31:00+00:00'))  # 00:01 IST
        self.assertTrue(ok, reason)
        self.assertEqual(after['calls_day'], 1)
        ledger = self.ledger(cfg)
        self.assertEqual(ledger['ist_day'], '2026-10-31')
        self.assertEqual(ledger['calls_day'], 1)
        self.assertTrue(budget.charge(cfg, 't5', 'on', now=_ts('2026-10-31T00:10:00+00:00'))[0])  # 05:40 IST
        denied = budget.charge(cfg, 't6', 'on', now=_ts('2026-10-31T00:20:00+00:00'))
        self.assertEqual(denied[:2], (False, 'budget_denied'), 'UTC midnight does not roll the day')

    def test_budget_counts_every_attempt(self):
        budget = self.mod('mr.judge.budget')
        cfg = self.cfg('count')
        ok, reason, after = budget.charge(cfg, 't1', 'shadow', now=T0)
        self.assertTrue(ok, reason)
        ledger = self.ledger(cfg)
        self.assertEqual(set(ledger), {'ist_day', 'calls_day', 'recent', 'calls_by_task'})
        self.assertEqual(ledger['ist_day'], '2026-10-30')
        self.assertEqual(ledger['calls_day'], 1)
        self.assertEqual(ledger['calls_by_task'], {'t1': 1})
        self.assertEqual(len(ledger['recent']), 1)
        stamp = datetime.fromisoformat(ledger['recent'][0])
        self.assertEqual(stamp.utcoffset(), timedelta(hours=5, minutes=30))
        self.assertEqual(stamp.timestamp(), T0)
        self.assertEqual(budget.calls_since(cfg, '2026-10-30T05:59:00Z'), 1)
        self.assertEqual(budget.calls_since(cfg, '2026-10-30T06:01:00+00:00'), 0)
        ok, reason, after = budget.charge(cfg, 't1', 'shadow', now=T0 + 30)
        self.assertTrue(ok, reason)
        self.assertEqual((after['calls_day'], after['calls_task']), (2, 2), 'a timed-out call already counted')


class TestJudgeRecent(LedgerCase):
    def test_rung_guard_reserved_includes_judge_recent(self):
        budget = self.mod('mr.judge.budget')
        cfg = self.cfg('g', check=gitfix.FAKE_CHECK)
        before = _ts('2026-10-29T23:50:00+00:00')
        after = _ts('2026-10-30T00:05:00+00:00')
        for i in range(2):
            self.assertTrue(budget.charge(cfg, f'old{i}', 'on', now=before + i)[0])
        for i in range(5):
            self.assertTrue(budget.charge(cfg, f'new{i}', 'on', now=after + i)[0])
        res = guard.check('S', cfg)
        self.assertTrue(res.allow, res)
        self.assertAlmostEqual(res.reserved, 1.0, msg='5 judge calls since checked_at x 0.2')
        cfg = self.cfg('g', {'est_pts_per_call': 0.5}, check=gitfix.FAKE_CHECK)
        res = guard.check('S', cfg)
        self.assertAlmostEqual(res.reserved, 2.5, msg='5 judge calls since checked_at x 0.5')


class TestBreaker(LedgerCase):
    def test_breaker_opens_after_three_failures_across_processes(self):
        breaker = self.mod('mr.judge.breaker')
        cfg = self.cfg()
        self.assertTrue(breaker.allow(cfg, 'haiku', now=T0))
        breaker.record(cfg, 'haiku', 'timeout', now=T0)
        breaker.record(cfg, 'haiku', 'cli_error', now=T0 + 1)
        self.assertTrue(breaker.allow(cfg, 'haiku', now=T0 + 2))
        code = NL.join([
            'import sys',
            'from mr import config',
            'from mr.defaults import defaults',
            'from mr.judge import breaker',
            'paths = dict(home=sys.argv[1], check=sys.argv[3], claude=sys.argv[4])',
            'cfg = config.from_dict(config._deep_merge(defaults(), dict(paths=paths)))',
            'breaker.record(cfg, sys.argv[5], sys.argv[6], now=float(sys.argv[2]))',
        ])
        proc = subprocess.run(
            [sys.executable, '-c', code, cfg.paths.home, repr(T0 + 3), '', 'claude', 'haiku', 'invalid_response'],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(os.path.join(cfg.paths.home, 'state', 'judge-breaker.json')))
        self.assertFalse(breaker.allow(cfg, 'haiku', now=T0 + 4), 'third failure came from another process')
        self.assertFalse(breaker.allow(cfg, 'haiku', now=T0 + 3 + 14 * 60))

    def test_breaker_half_open_trial(self):
        breaker = self.mod('mr.judge.breaker')
        cfg = self.cfg()
        for i, status in enumerate(('timeout', 'model_drift', 'cli_error')):
            breaker.record(cfg, 'haiku', status, now=T0 + i)
        opened = T0 + 2
        self.assertFalse(breaker.allow(cfg, 'haiku', now=opened + 60))
        self.assertFalse(breaker.allow(cfg, 'haiku', now=opened + 15 * 60 - 1))
        self.assertTrue(breaker.allow(cfg, 'haiku', now=opened + 15 * 60 + 1), 'one half-open trial')
        self.assertFalse(breaker.allow(cfg, 'haiku', now=opened + 15 * 60 + 2), 'only one trial at a time')
        breaker.record(cfg, 'haiku', 'ok', now=opened + 15 * 60 + 3)
        self.assertTrue(breaker.allow(cfg, 'haiku', now=opened + 15 * 60 + 4), 'a successful trial closes it')
        self.assertTrue(breaker.allow(cfg, 'haiku', now=opened + 15 * 60 + 5))
        cfg = self.cfg('b')
        for i in range(3):
            breaker.record(cfg, 'haiku', 'timeout', now=T0 + i)
        trial = T0 + 2 + 15 * 60 + 1
        self.assertTrue(breaker.allow(cfg, 'haiku', now=trial))
        breaker.record(cfg, 'haiku', 'timeout', now=trial + 1)
        self.assertFalse(breaker.allow(cfg, 'haiku', now=trial + 60), 'a failed trial reopens the breaker')

    def test_breaker_ignores_yield_and_budget_per_backend(self):
        breaker = self.mod('mr.judge.breaker')
        cfg = self.cfg()
        statuses = ('timeout', 'timeout', 'guard_yield', 'budget_denied', 'guard_yield', 'budget_denied')
        for i, status in enumerate(statuses):
            breaker.record(cfg, 'haiku', status, now=T0 + i)
        self.assertTrue(breaker.allow(cfg, 'haiku', now=T0 + 10), 'guard_yield and budget_denied are not failures')
        for i, status in enumerate(('timeout', 'invalid_response', 'ok', 'timeout', 'cli_error')):
            breaker.record(cfg, 'flash', status, now=T0 + i)
        self.assertTrue(breaker.allow(cfg, 'flash', now=T0 + 10), 'a success resets the count')
        for i in range(3):
            breaker.record(cfg, 'jev', 'timeout', now=T0 + i)
        self.assertFalse(breaker.allow(cfg, 'jev', now=T0 + 10))
        self.assertTrue(breaker.allow(cfg, 'clef', now=T0 + 10), 'breakers are per backend')
        self.assertTrue(breaker.allow(cfg, 'haiku', now=T0 + 10))


class TestCache(LedgerCase):
    def parts(self, **over):
        base = {'use': 'route', 'backend': 'haiku', 'model': HAIKU, 'effort': 'low', 'prompt_sha': 'p' * 64,
                'qset_sha': 'q' * 64, 'state_sha': 's' * 64}
        base.update(over)
        return base

    def test_cache_hit_ttl_and_no_state(self):
        cache = self.mod('mr.judge.cache')
        cfg = self.cfg()
        canary = 'STATE-CANARY-' + 'x1y2z3'
        state_sha = hashlib.sha256(canary.encode('utf-8')).hexdigest()
        key = cache.make_key(**self.parts(state_sha=state_sha))
        self.assertIsNone(cache.get(cfg, key, now=T0))
        answers = {'q01': {'answer': True, 'confidence': 0.9}}
        cache.put(cfg, key, use='route', backend='haiku', qset_sha='q' * 64, answers=answers,
                  usage={'input_tokens': 120, 'output_tokens': 14}, model_served=HAIKU, now=T0)
        hit = cache.get(cfg, key, now=T0 + 7 * DAY - 60)
        self.assertIsNotNone(hit, 'identical key is a hit inside the TTL')
        self.assertEqual(hit['answers'], answers)
        self.assertEqual(hit['model_served'], HAIKU)
        db = os.path.join(cfg.paths.home, 'state', 'judge-cache.sqlite')
        self.assertTrue(os.path.exists(db))
        with contextlib.closing(sqlite3.connect(db)) as conn:
            cols = [row[1] for row in conn.execute('PRAGMA table_info(judge_cache)')]
            rows = conn.execute('SELECT * FROM judge_cache').fetchall()
        self.assertEqual(cols, COLS)
        self.assertEqual(len(rows), 1)
        self.assertNotIn('STATE-CANARY', repr(rows))
        row = dict(zip(cols, rows[0]))
        self.assertEqual((row['key'], row['use'], row['backend']), (key, 'route', 'haiku'))
        self.assertEqual(json.loads(row['answers_json']), answers)
        self.assertIsNone(cache.get(cfg, key, now=T0 + 7 * DAY + 60), 'expired after route_ttl_days')
        self.assertEqual(cache.purge_expired(cfg, now=T0 + 8 * DAY), 1)
        with contextlib.closing(sqlite3.connect(db)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM judge_cache').fetchone()[0], 0)

    def test_cache_key_changes_with_each_field(self):
        cache = self.mod('mr.judge.cache')
        base = self.parts()
        k0 = cache.make_key(**base)
        joined = '|'.join([base['use'], base['backend'], base['model'], base['effort'], base['prompt_sha'],
                           base['qset_sha'], base['state_sha']])
        self.assertEqual(k0, hashlib.sha256(joined.encode('utf-8')).hexdigest())
        self.assertEqual(cache.make_key(**base), k0)
        seen = {k0}
        changes = (('use', 'spec'), ('backend', 'flash'), ('model', 'other-model'), ('effort', None),
                   ('prompt_sha', 'p2'), ('qset_sha', 'q2'), ('state_sha', 's2'))
        for field, value in changes:
            key = cache.make_key(**self.parts(**{field: value}))
            self.assertNotIn(key, seen, field)
            seen.add(key)
        cfg = self.cfg()
        cache.put(cfg, k0, use='route', backend='haiku', qset_sha='q' * 64, answers={'q01': None},
                  usage={}, model_served=HAIKU, now=T0)
        self.assertIsNotNone(cache.get(cfg, k0, now=T0 + 1))
        self.assertIsNone(cache.get(cfg, cache.make_key(**self.parts(qset_sha='q2')), now=T0 + 1))


if __name__ == '__main__':
    unittest.main()
