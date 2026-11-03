'''DROID1: S/S+ prefer droid in `mr run` (--once and the loop), sticky fallback to the claude CLI, `mr droid`.

Fake droid, fake claude and fake cursor-agent only; temp MR_HOME; no network.
'''
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from mr import cli
from tests import gitfix
from tests.test_droid1_core_mr import AUTH_MSG, CREDITS_MSG, KEY, RATE_MSG, exec_calls, make_fake_droid, write_steps
from tests.test_loop_run import GH, GM, GX, OPUS, PLAN, README, REVIEW_PASS, SONNET, LoopCase, mul
from tests.test_run_once import CHECK_DEFAULT

FAR_PAST, FAR_FUTURE = 946684800.0, 4102444800.0
JSON_KEYS = {'installed', 'version', 'enabled', 'key_present', 'settings', 'state', 'status',
             'latency_ms', 'usage', 'detail'}
DENY_5H = {'default': dict(CHECK_DEFAULT, five_hour_pct=84)}
FAIL = {'is_error': True, 'rc': 1, 'num_turns': 0}


class DroidRunCase(LoopCase):
    def fake(self):
        if not hasattr(self, 'droid_bin'):
            self.droid_bin, self.droid_steps_path = make_fake_droid(self.dir)
        return self.droid_bin, self.droid_steps_path

    def set_scenario(self, scenario, check=None):
        super().set_scenario(scenario, check)
        self.env.update({'ANTHROPIC_API_KEY': KEY, 'DROID1_FAKE': self.fake()[1], 'MR_HOME': self.home})

    def write_cfg(self):
        raw = {'paths': {'home': self.home, 'cursor_agent': gitfix.FAKE_CURSOR, 'claude': gitfix.FAKE_CLAUDE,
                         'check': gitfix.FAKE_CHECK, 'droid': self.fake()[0]},
               'hygiene': {'commands': {'repo': self.hygiene_cmds}},
               'acceptance': {'runners': {'repo': 'unittest'}},
               'droid': {'enabled': True, 'recheck_s': 3600}, 'judge': {'mode': 'off'}}
        with open(self.cfgpath, 'w', encoding='utf-8') as fh:
            json.dump(raw, fh)

    def droid_steps(self, *steps):
        write_steps(self.fake()[1], list(steps))

    def droid_execs(self):
        return exec_calls(self.fake()[1])

    def cli_calls(self, name):
        return [c for c in self.calls() if c['cli'] == name]

    def impl(self):
        return self.records('implementer')

    def once(self):
        return self.run_json('--rung', 'S', '--once')

    def state_file(self):
        return os.path.join(self.home, 'state', 'droid.json')

    def droid_state(self):
        if not os.path.isfile(self.state_file()):
            return {'status': 'ok'}
        with open(self.state_file(), encoding='utf-8') as fh:
            return json.load(fh)

    def put_state(self, status, recheck_after):
        os.makedirs(os.path.dirname(self.state_file()), exist_ok=True)
        with open(self.state_file(), 'w', encoding='utf-8') as fh:
            json.dump({'status': status, 'since': FAR_PAST, 'recheck_after': recheck_after,
                       'detail': 'credit balance is too low'}, fh)

    def droid_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        env = dict(self.env, MR_TEST_TIME_SCALE='1')
        with patch.dict(os.environ, env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['--config', self.cfgpath, '--json', 'droid', *args])
        return code, out.getvalue(), err.getvalue()

    def doc_of(self, out):
        self.assertTrue(out.strip().startswith('{'), f'no JSON object on stdout: {out!r}')
        return json.loads(out)


class TestDroidFirst(DroidRunCase):
    def test_once_s_runs_on_droid_despite_five_hour_deny(self):
        self.set_scenario({}, check=DENY_5H)
        self.droid_steps({'files': README})
        code, doc, err = self.once()
        self.assertEqual(len(self.droid_execs()), 1, err)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.cli_calls('claude'), [])
        self.assertEqual(self.records('guard'), [])
        impl = self.impl()
        self.assertEqual(len(impl), 1)
        self.assertEqual((impl[0].get('backend'), impl[0]['rung'], impl[0]['model'], impl[0]['in_tok']),
                         ('droid', 'S', SONNET, 100))
        self.assertEqual(self.state_of(doc['task'])['attempts'], 1)
        self.assertNotIn(KEY, err + json.dumps(doc))
        self.assert_schema()

    def test_loop_s_runs_on_droid_when_guard_denies(self):
        self.set_scenario({'cursor': {GH: [{'result': json.dumps(PLAN)}], GX: [mul('+'), {'result': REVIEW_PASS}]}},
                          check=DENY_5H)
        self.droid_steps(mul('*', '  # fixed'))
        code, doc, err = self.run_json('--type', 'bugfix', '--difficulty', 'medium')
        self.assertEqual(len(self.droid_execs()), 1, err)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.cli_calls('claude'), [])
        st = self.state_of(doc['task'])
        self.assertEqual([(r['rung'], r['class']) for r in st['rungs']], [('G-x', 'tried_wrong'), ('S', None)])
        self.assertEqual([r.get('backend') for r in self.impl()], ['cursor', 'droid'])
        self.assert_schema()

    def test_ordinary_droid_failure_escalates_without_fallback(self):
        self.set_scenario({'cursor': {GM: [{'files': README}]}})
        self.droid_steps({'files': {'other/zzz.txt': 'elsewhere'}})
        code, doc, err = self.run_json('--type', 'code', '--difficulty', 'easy', '--rung', 'S')
        self.assertEqual(len(self.droid_execs()), 1, err)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.cli_calls('claude'), [])
        st = self.state_of(doc['task'])
        self.assertEqual([(r['rung'], r['class']) for r in st['rungs']], [('S', 'off_track'), ('G-m', None)])
        self.assertEqual(st['attempts'], 2)
        self.assertEqual([r.get('backend') for r in self.impl()], ['droid', 'cursor'])
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'other', 'zzz.txt')))
        self.assertEqual(self.droid_state()['status'], 'ok')
        self.assert_schema()


class TestFallback(DroidRunCase):
    def test_credits_falls_back_in_same_attempt_and_sticks(self):
        self.set_scenario({'claude': {SONNET: [{'files': README}]}})
        self.droid_steps(dict(FAIL, files={'droid-junk.txt': 'junk'}, result=CREDITS_MSG))
        code, doc, err = self.once()
        self.assertEqual(len(self.droid_execs()), 1, err)
        self.assertEqual(code, 0, err)
        claude = self.cli_calls('claude')
        self.assertEqual(len(claude), 1)
        self.assertNotIn('ANTHROPIC_API_KEY', claude[0]['env']['env_keys'])
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'droid-junk.txt')))
        st = self.droid_state()
        self.assertEqual(st['status'], 'credits_exhausted')
        self.assertAlmostEqual(st['recheck_after'] - st['since'], 3600, delta=1)
        self.assertLessEqual(len(st.get('detail') or ''), 200)
        lines = [ln for ln in err.splitlines() if ln.startswith('droid:')]
        self.assertEqual(len(lines), 1, err)
        self.assertTrue(lines[0].startswith('droid: credits_exhausted, falling back to the claude CLI until'),
                        lines[0])
        task = self.state_of(doc['task'])
        self.assertEqual((task['attempts'], task['claude_rungs'], len(task['rungs'])), (1, 1, 1))
        impl = self.impl()
        self.assertEqual(len(impl), 1)
        self.assertEqual(impl[0].get('backend'), 'claude')
        sub = impl[0].get('droid_try') or {}
        self.assertEqual((sub.get('backend'), sub.get('fallback')), ('droid', 'credits'))
        with open(self.state_file(), encoding='utf-8') as fh:
            self.assertNotIn(KEY, err + json.dumps(doc) + fh.read())
        self.assert_schema()

    def test_transient_falls_back_without_sticky_state(self):
        self.set_scenario({'claude': {SONNET: [{'files': README}]}})
        self.droid_steps(dict(FAIL, result=RATE_MSG))
        code, doc, err = self.once()
        self.assertEqual(len(self.droid_execs()), 1, err)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.cli_calls('claude')), 1)
        self.assertEqual(self.droid_state()['status'], 'ok')
        impl = self.impl()
        self.assertEqual([r.get('backend') for r in impl], ['claude'])
        self.assertEqual((impl[0].get('droid_try') or {}).get('fallback'), 'transient')
        self.assertEqual(self.state_of(doc['task'])['attempts'], 1)
        self.assert_schema()

    def test_within_recheck_window_skips_droid(self):
        self.put_state('credits_exhausted', FAR_FUTURE)
        self.set_scenario({'claude': {SONNET: [{'files': README}]}})
        code, doc, err = self.once()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.droid_execs(), [])
        self.assertEqual(len(self.cli_calls('claude')), 1)
        impl = self.impl()
        self.assertEqual([r.get('backend') for r in impl], ['claude'])
        self.assertIsNone(impl[0].get('droid_try'))
        self.assertTrue(self.records('guard'))
        self.assertEqual(self.droid_state()['status'], 'credits_exhausted')

    def test_after_recheck_droid_is_retried_and_state_resets(self):
        self.put_state('auth_failed', FAR_PAST)
        self.droid_steps({'files': README})
        code, doc, err = self.once()
        self.assertEqual(len(self.droid_execs()), 1, err)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.cli_calls('claude'), [])
        self.assertEqual(self.droid_state()['status'], 'ok')
        self.assertEqual([r.get('backend') for r in self.impl()], ['droid'])

    def test_planner_and_reviewer_never_use_droid(self):
        self.set_scenario({'cursor': {GX: [mul('*')]},
                           'claude': {OPUS: [{'result': json.dumps(PLAN)}], SONNET: [{'result': REVIEW_PASS}]}})
        self.droid_steps({'files': README})
        code, doc, err = self.run_json('--type', 'bugfix', '--difficulty', 'hard')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.droid_execs(), [])
        self.assertEqual(self.models(), [OPUS, GX, SONNET])
        self.assertEqual([r.get('backend') for r in self.impl()], ['cursor'])


class TestDroidCommand(DroidRunCase):
    def test_check_missing_binary_exits_2(self):
        with open(self.cfgpath, encoding='utf-8') as fh:
            raw = json.load(fh)
        raw['paths']['droid'] = os.path.join(self.dir, 'no-such-droid')
        with open(self.cfgpath, 'w', encoding='utf-8') as fh:
            json.dump(raw, fh)
        code, out, err = self.droid_cli('check')
        self.assertEqual(code, 2, err)
        self.assertIs(self.doc_of(out)['installed'], False)
        self.assertEqual(self.droid_execs(), [])

    def test_check_without_key_exits_3_and_never_spawns(self):
        self.env['ANTHROPIC_API_KEY'] = ''
        code, out, err = self.droid_cli('check', '--probe')
        self.assertEqual(code, 3, err)
        self.assertEqual(self.droid_execs(), [])
        self.assertIs(self.doc_of(out)['key_present'], False)

    def test_probe_ok_resets_state_and_prints_json(self):
        self.put_state('credits_exhausted', FAR_FUTURE)
        self.droid_steps({'result': 'ok'})
        code, out, err = self.droid_cli('check', '--probe')
        self.assertEqual(code, 0, err)
        doc = self.doc_of(out)
        self.assertEqual(set(doc), JSON_KEYS)
        self.assertEqual((doc['installed'], doc['enabled'], doc['key_present'], doc['status']),
                         (True, True, True, 'ok'))
        self.assertIn('1.1.0', str(doc['version']))
        self.assertIsInstance(doc['latency_ms'], int)
        self.assertIsInstance(doc['usage'], dict)
        self.assertEqual(self.droid_state()['status'], 'ok')
        execs = self.droid_execs()
        self.assertEqual(len(execs), 1)
        argv = execs[0]['argv']
        self.assertEqual(argv[argv.index('-m') + 1], 'custom:mr-sonnet-high-0')
        self.assertNotIn('--auto', argv)
        self.assertEqual(execs[0]['keys'], ['ANTHROPIC_API_KEY'])
        self.assertNotIn(KEY, out + err)

    def test_probe_credits_and_auth_exit_codes(self):
        for msg, want_code, want_state, want_status in ((CREDITS_MSG, 5, 'credits_exhausted', 'credits'),
                                                        (AUTH_MSG, 4, 'auth_failed', 'auth')):
            if os.path.exists(self.state_file()):
                os.remove(self.state_file())
            self.droid_steps(dict(FAIL, result=msg))
            code, out, err = self.droid_cli('check', '--probe')
            self.assertEqual(code, want_code, (msg, err))
            self.assertEqual(self.doc_of(out)['status'], want_status)
            self.assertEqual(self.droid_state()['status'], want_state)
            self.assertNotIn(KEY, out + err)

    def test_reset_clears_sticky_state(self):
        self.put_state('auth_failed', FAR_FUTURE)
        code, out, err = self.droid_cli('reset')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.droid_state()['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
