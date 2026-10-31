# SHD6 locked tests: guard.run_claude_judge, the judge client status mapping and the judge budget config.
import glob
import importlib
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from mr import config, guard
from mr.defaults import defaults
from tests import gitfix

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HAIKU = 'claude-haiku-5-5'
DENY = 'Bash Edit Write Read Grep Glob WebFetch WebSearch Task NotebookEdit'
NL = chr(10)
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


def _envelope(result=None, models=(HAIKU,), is_error=False):
    text = json.dumps({'answers': {}}) if result is None else result
    return json.dumps({
        'type': 'result', 'subtype': 'success', 'is_error': is_error, 'result': text,
        'duration_ms': 2100, 'duration_api_ms': 1335, 'total_cost_usd': 0.0017,
        'usage': {'input_tokens': 120, 'output_tokens': 14},
        'modelUsage': {m: {'inputTokens': 120, 'outputTokens': 14} for m in models},
    })


def _alive(pid):
    try:
        with open(f'/proc/{pid}/stat', encoding='utf-8') as fh:
            state = fh.read().rsplit(')', 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ('Z', 'X')


class Recorder:
    # Stands in for runners.exec_child: (argv, cwd, env, timeout_s, raw_base) -> (rc, out, err, wall_s, timed_out).
    def __init__(self, rc=0, out='', err='', timed_out=False):
        self.calls = []
        self.reply = (rc, out, err, 0.05, timed_out)

    def __call__(self, argv, cwd, env, timeout_s, raw_base):
        self.calls.append({'argv': list(argv), 'cwd': cwd, 'env': dict(env), 'timeout_s': timeout_s})
        return self.reply


class Shd6Case(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.home = os.path.realpath(td.name)
        self.wrapper = self.write_wrapper(['#!/bin/sh', 'exit 0'])
        self.set_check({'default': dict(CHECK)})
        patcher = mock.patch.dict(os.environ, {
            'MR_HOME': self.home, 'MR_FAKE_CHECK': self.check_path, 'MR_TEST_TIME_SCALE': '1',
            'CLOUDFLARE_SHD6_PROBE': 'x'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_wrapper(self, lines, name='claude', mode=0o755):
        folder = os.path.join(self.home, 'bin')
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(NL.join(lines) + NL)
        os.chmod(path, mode)
        return path

    def set_check(self, doc):
        self.check_path = gitfix.write_check(self.home, doc)
        if os.path.exists(self.check_path + '.calls'):
            os.remove(self.check_path + '.calls')

    def check_calls(self):
        if not os.path.exists(self.check_path + '.calls'):
            return []
        with open(self.check_path + '.calls', encoding='utf-8') as fh:
            return fh.read().splitlines()

    def cfg(self, check=None, claude=None, judge=None):
        over = {'paths': {'home': self.home, 'check': gitfix.FAKE_CHECK if check is None else check,
                          'claude': self.wrapper if claude is None else claude}}
        if judge is not None:
            over['judge'] = judge
        return config.from_dict(config._deep_merge(defaults(), over))

    def mod(self, name):
        try:
            return importlib.import_module(name)
        except ModuleNotFoundError as exc:
            self.fail(f'{name} is missing: {exc}')

    def judge_fn(self):
        fn = getattr(guard, 'run_claude_judge', None)
        self.assertTrue(callable(fn), 'guard.run_claude_judge is missing')
        return fn

    def demoted_path(self):
        return os.path.join(self.home, 'state', 'judge-demoted.json')


class TestRunClaudeJudge(Shd6Case):
    def test_judge_argv_golden(self):
        run = self.judge_fn()
        rec = Recorder(0, _envelope())
        res = run('PROMPT-X', self.cfg(), 12.0, mode='on', exec_fn=rec)
        self.assertNotEqual(res.status, 'guard_yield')
        self.assertEqual(len(rec.calls), 1)
        call = rec.calls[0]
        self.assertEqual(call['argv'], [self.wrapper, '-p', 'PROMPT-X', '--model', HAIKU, '--effort', 'low',
                                        '--output-format', 'json', '--no-session-persistence',
                                        '--disallowedTools', DENY])
        self.assertNotIn('--bare', call['argv'])
        workdir = os.path.join(self.home, 'judge-workdir')
        self.assertTrue(os.path.isdir(workdir))
        self.assertEqual(os.path.realpath(call['cwd']), workdir)
        self.assertEqual(call['env'].get('CLAUDE_CODE_DISABLE_ADVISOR_TOOL'), '1')
        self.assertNotIn('CLOUDFLARE_SHD6_PROBE', call['env'])
        self.assertEqual(call['timeout_s'], 12.0)
        rec = Recorder(0, _envelope())
        run('P', self.cfg(judge={'backends': {'haiku': {'effort': None}}}), 12.0, mode='on', exec_fn=rec)
        self.assertEqual(len(rec.calls), 1)
        argv = rec.calls[0]['argv']
        self.assertNotIn('--effort', argv)
        self.assertEqual(argv, [self.wrapper, '-p', 'P', '--model', HAIKU, '--output-format', 'json',
                                '--no-session-persistence', '--disallowedTools', DENY])
        for path in glob.glob(os.path.join(ROOT, 'mr', 'judge', '**', '*'), recursive=True):
            if not os.path.isfile(path) or '__pycache__' in path:
                continue
            rel = os.path.relpath(path, ROOT)
            with open(path, encoding='utf-8') as fh:
                text = fh.read()
            for word in ('cursor_agent', 'run_cursor', 'cursor-agent'):
                self.assertNotIn(word, text, f'{rel} mentions {word}')

    def test_yields_at_five_hour_max_in_any_mode(self):
        run = self.judge_fn()
        self.set_check({'default': {**CHECK, 'five_hour_pct': 85.0}})
        for mode in ('on', 'shadow'):
            rec = Recorder(0, _envelope())
            res = run('P', self.cfg(), 12.0, mode=mode, exec_fn=rec)
            self.assertEqual(res.status, 'guard_yield', mode)
            self.assertEqual(rec.calls, [], mode)
        calls = self.check_calls()
        self.assertTrue(calls, 'the judge reads check.sh before deciding')
        self.assertFalse(any('--probe' in c.split() for c in calls), calls)

    def test_shadow_yields_at_shadow_five_hour_max(self):
        run = self.judge_fn()
        self.set_check({'default': {**CHECK, 'five_hour_pct': 80.0}})
        rec = Recorder(0, _envelope())
        self.assertEqual(run('P', self.cfg(), 12.0, mode='shadow', exec_fn=rec).status, 'guard_yield')
        self.assertEqual(rec.calls, [])
        rec = Recorder(0, _envelope())
        self.assertNotEqual(run('P', self.cfg(), 12.0, mode='on', exec_fn=rec).status, 'guard_yield')
        self.assertEqual(len(rec.calls), 1, 'mode on still calls at 80')
        self.set_check({'default': {**CHECK, 'five_hour_pct': 79.0}})
        rec = Recorder(0, _envelope())
        self.assertNotEqual(run('P', self.cfg(), 12.0, mode='shadow', exec_fn=rec).status, 'guard_yield')
        self.assertEqual(len(rec.calls), 1, 'shadow calls below 80')
        self.set_check({'default': {**CHECK, 'five_hour_pct': 75.0}})
        cfg70 = self.cfg(judge={'budget': {'shadow_five_hour_max': 70}})
        rec = Recorder(0, _envelope())
        self.assertEqual(run('P', cfg70, 12.0, mode='shadow', exec_fn=rec).status, 'guard_yield')
        self.assertEqual(rec.calls, [])
        rec = Recorder(0, _envelope())
        self.assertNotEqual(run('P', cfg70, 12.0, mode='on', exec_fn=rec).status, 'guard_yield')
        self.assertEqual(len(rec.calls), 1)

    def test_failing_unusable_or_empty_check(self):
        run = self.judge_fn()
        cases = (
            ('check exits 1', {'default': dict(CHECK), 'no_probe': 'fail', 'probe': 'fail'}),
            ('check prints junk', {'default': dict(CHECK), 'no_probe': 'junk', 'probe': 'junk'}),
            ('no five_hour_pct', {'default': {**CHECK, 'five_hour_pct': None}}),
        )
        for name, doc in cases:
            self.set_check(doc)
            rec = Recorder(0, _envelope())
            res = run('P', self.cfg(), 12.0, mode='on', exec_fn=rec)
            self.assertEqual(res.status, 'guard_yield', name)
            self.assertEqual(rec.calls, [], name)
            self.assertFalse(any('--probe' in c.split() for c in self.check_calls()), f'{name}: never probes')
        rec = Recorder(0, _envelope())
        res = run('P', self.cfg(check=''), 12.0, mode='shadow', exec_fn=rec)
        self.assertNotEqual(res.status, 'guard_yield', 'empty check path follows the rung guard (no check)')
        self.assertEqual(len(rec.calls), 1)

    def test_never_takes_claude_lock_or_reserves(self):
        run = self.judge_fn()
        rec = Recorder(0, _envelope())
        run('P', self.cfg(), 12.0, mode='on', exec_fn=rec)
        self.assertEqual(len(rec.calls), 1)
        state = os.path.join(self.home, 'state')
        for name in ('claude.lock', 'reservations.json'):
            self.assertFalse(os.path.exists(os.path.join(state, name)), name)
        self.assertFalse(any('--probe' in c.split() for c in self.check_calls()), 'no probe after the call')
        rec = Recorder(75, '', 'hourly limit reached')
        res = run('P', self.cfg(), 12.0, mode='on', exec_fn=rec)
        self.assertEqual(res.status, 'guard_yield')
        self.assertEqual(len(rec.calls), 1)
        for name in ('claude.lock', 'reservations.json'):
            self.assertFalse(os.path.exists(os.path.join(state, name)), name)


class TestJudgeClient(Shd6Case):
    def test_client_ok_maps_raw_result(self):
        client = self.mod('mr.judge.client')
        text = json.dumps({'answers': {'q01': {'answer': True, 'confidence': 0.9}}})
        rec = Recorder(0, _envelope(result=text))
        raw = client.call('P', self.cfg(), mode='on', exec_fn=rec)
        self.assertEqual(raw.status, 'ok')
        self.assertEqual(raw.result_text, text)
        self.assertEqual(raw.exit_code, 0)
        self.assertEqual(raw.duration_api_ms, 1335)
        self.assertEqual((raw.input_tokens, raw.output_tokens), (120, 14))
        self.assertAlmostEqual(raw.usd_equiv, 0.0017)
        self.assertEqual(raw.model_served, HAIKU)
        self.assertIsInstance(raw.latency_ms, int)
        self.assertGreaterEqual(raw.latency_ms, 0)
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(rec.calls[0]['timeout_s'], 12.0)
        self.assertFalse(os.path.exists(self.demoted_path()))
        rec = Recorder(0, _envelope(result=text))
        client.call('P', self.cfg(), mode='shadow', timeout_ms=3000, exec_fn=rec)
        self.assertEqual(rec.calls[0]['timeout_s'], 3.0)

    def test_client_fail_open_table(self):
        client = self.mod('mr.judge.client')
        cases = (
            ('exit 1', Recorder(1, _envelope(), 'boom'), 'cli_error'),
            ('exit 75', Recorder(75, '', 'hourly limit reached'), 'guard_yield'),
            ('is_error true', Recorder(0, _envelope(is_error=True)), 'cli_error'),
            ('non-JSON envelope', Recorder(0, 'not json at all'), 'cli_error'),
            ('envelope not an object', Recorder(0, json.dumps([1, 2])), 'cli_error'),
        )
        for name, rec, want in cases:
            raw = client.call('P', self.cfg(), mode='on', exec_fn=rec)
            self.assertEqual(raw.status, want, name)
            self.assertEqual(len(rec.calls), 1, f'{name}: exactly one call, no retries')
        self.set_check({'default': {**CHECK, 'five_hour_pct': 90.0}})
        rec = Recorder(0, _envelope())
        raw = client.call('P', self.cfg(), mode='on', exec_fn=rec)
        self.assertEqual(raw.status, 'guard_yield')
        self.assertEqual(rec.calls, [])

    def test_client_model_drift_writes_demoted(self):
        client = self.mod('mr.judge.client')
        cases = (
            ('other model', ('claude-sonnet-5-5',)),
            ('extra model', (HAIKU, 'claude-opus-5-5')),
            ('no model', ()),
        )
        for name, models in cases:
            if os.path.exists(self.demoted_path()):
                os.remove(self.demoted_path())
            raw = client.call('P', self.cfg(), mode='on', exec_fn=Recorder(0, _envelope(models=models)))
            self.assertEqual(raw.status, 'model_drift', name)
            self.assertTrue(os.path.exists(self.demoted_path()), name)
            with open(self.demoted_path(), encoding='utf-8') as fh:
                doc = json.load(fh)
            self.assertEqual(doc.get('reason'), 'model_drift', name)
            self.assertEqual(sorted(doc.get('served') or []), sorted(models), name)
            self.assertIn('ts', doc, name)

    def test_client_timeout_kills_process_group(self):
        client = self.mod('mr.judge.client')
        pid_file = os.path.join(self.home, 'child.pid')
        slow = self.write_wrapper(['#!/bin/sh', 'sleep 30 &', f'echo $! > {pid_file}', 'wait'], name='slow-claude')
        start = time.monotonic()
        raw = client.call('P', self.cfg(claude=slow), mode='on', timeout_ms=1000)
        elapsed = time.monotonic() - start
        self.assertEqual(raw.status, 'timeout')
        self.assertLess(elapsed, 15)
        self.assertTrue(os.path.exists(pid_file), 'the slow wrapper started its child')
        with open(pid_file, encoding='utf-8') as fh:
            pid = int(fh.read().strip())
        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_alive(pid), f'child {pid} of the timed-out group is still running')

    def test_client_cli_unavailable(self):
        client = self.mod('mr.judge.client')
        missing = os.path.join(self.home, 'bin', 'no-such-claude')
        plain = self.write_wrapper(['#!/bin/sh', 'exit 0'], name='plain-claude', mode=0o644)
        for name, path in (('missing wrapper', missing), ('not executable', plain)):
            rec = Recorder(0, _envelope())
            raw = client.call('P', self.cfg(claude=path), mode='on', exec_fn=rec)
            self.assertEqual(raw.status, 'cli_unavailable', name)
            self.assertEqual(rec.calls, [], name)


class TestJudgeBudgetConfig(Shd6Case):
    def test_judge_budget_config(self):
        budget = self.cfg().judge['budget']
        self.assertIsNone(budget.get('calls_per_hour', 'absent'), 'calls_per_hour defaults to null (no hourly cap)')
        self.assertEqual(budget.get('shadow_five_hour_max'), 80)
        self.assertEqual((budget.get('calls_per_day'), budget.get('max_calls_per_task')), (150, 6))
        cfg = self.cfg(judge={'budget': {'calls_per_hour': 20, 'shadow_five_hour_max': 100}})
        self.assertEqual(cfg.judge['budget']['calls_per_hour'], 20)
        self.assertEqual(cfg.judge['budget']['shadow_five_hour_max'], 100)
        bad = (
            ('shadow 101', {'shadow_five_hour_max': 101}),
            ('shadow negative', {'shadow_five_hour_max': -1}),
            ('shadow text', {'shadow_five_hour_max': 'high'}),
            ('hourly text', {'calls_per_hour': 'lots'}),
        )
        for name, over in bad:
            with self.assertRaises(config.ConfigError, msg=name):
                self.cfg(judge={'budget': over})


if __name__ == '__main__':
    unittest.main()
