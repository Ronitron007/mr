'''AJ8: mr.checkjudge (state, prompt, strict reply), the plan judge rung, the check_judge config and log role.'''
import copy
import importlib
import json
import os
import tempfile
import unittest
from unittest import mock

from mr import config
from mr.config import ConfigError
from mr.defaults import defaults
from tests import gitfix, log_schema_v2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SONNET = 'claude-sonnet-5-5'
TOKEN = 'sk-' + 'FAKEfake' * 4  # fake test value, built by concatenation
FINDING = 'tests/test_mul_mr.py::test_mul_zero passed on base'
TASK = 'Add mul to the repo.'
PLAN = {
    'goal': 'Add mul.',
    'scope': {'modify': ['README.md'], 'create': ['mul.py', 'tests/test_mul_mr.py'], 'forbidden': []},
    'requirements': [{'id': 'R1', 'text': 'mul SHALL multiply',
                      'examples': [{'given': '2, 3', 'when': 'mul', 'then': '6'}]}],
    'acceptance': [{'id': 'A1', 'covers': ['R1'], 'file': 'tests/test_mul_mr.py', 'tests': ['test_mul_basic']}],
    'est_loc': 10,
    'files_map': {'R1': ['mul.py']},
    'tests': [{'path': 'tests/test_mul_mr.py', 'content': 'import unittest\n'}],
}
STATE_KEYS = {'kind', 'check', 'task', 'plan', 'findings', 'signals', 'diff', 'attempt'}
WANT = {'plan_rung': 'S', 'plan_rung_strong': 'S+', 'max_plan_calls': 2, 'timeout_s': 300}


def reply(**kw):
    doc = {'verdict': 'accept', 'reason': 'The gap does not matter for this task.',
           'instructions': [], 'findings_ok': [FINDING]}
    doc.update(kw)
    return json.dumps(doc)


class Case(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.home = os.path.join(os.path.realpath(td.name), 'home')
        patcher = mock.patch.dict(os.environ, {'MR_HOME': self.home, 'MR_TEST_ALLOW_FAKE_PATHS': '1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def cj(self):
        try:
            return importlib.import_module('mr.checkjudge')
        except ModuleNotFoundError as exc:
            self.fail(f'mr.checkjudge is missing: {exc}')

    def cfg(self, extra=None):
        raw = config._deep_merge(defaults(), {
            'paths': {'home': self.home, 'cursor_agent': gitfix.FAKE_CURSOR,
                      'claude': gitfix.FAKE_CLAUDE, 'check': gitfix.FAKE_CHECK}})
        return config.from_dict(config._deep_merge(raw, extra or {}))

    def block(self, cfg):
        got = getattr(cfg, 'check_judge', None)
        self.assertIsInstance(got, dict, 'Config.check_judge is the validated check_judge block')
        return got


class TestParse(Case):
    def test_parse_accepts_each_verdict(self):
        parse = self.cj().parse
        for verdict in ('accept', 'repair', 'reject'):
            got = parse('\n' + reply(verdict=verdict, instructions=['move a.py to create']) + '\n')
            self.assertIsNotNone(got, verdict)
            self.assertEqual(got.verdict, verdict)
            self.assertEqual(got.reason, 'The gap does not matter for this task.')
            self.assertEqual(list(got.instructions), ['move a.py to create'])
            self.assertEqual(list(got.findings_ok), [FINDING])

    def test_parse_rejects_extra_or_missing_key(self):
        parse = self.cj().parse
        self.assertIsNone(parse(reply(note='extra')))
        doc = json.loads(reply())
        del doc['findings_ok']
        self.assertIsNone(parse(json.dumps(doc)))

    def test_parse_rejects_two_objects_or_no_json(self):
        parse = self.cj().parse
        self.assertIsNone(parse(reply() + '\n' + reply(verdict='reject')))
        self.assertIsNone(parse('not json'))
        self.assertIsNone(parse('[' + reply() + ']'))

    def test_parse_rejects_bad_verdict(self):
        parse = self.cj().parse
        for bad in ('maybe', 'ACCEPT', '', None, 1):
            self.assertIsNone(parse(reply(verdict=bad)), repr(bad))

    def test_parse_reason_cap(self):
        parse = self.cj().parse
        self.assertIsNotNone(parse(reply(reason='x' * 600)))
        for bad in ('x' * 601, '', None):
            self.assertIsNone(parse(reply(reason=bad)), repr(bad)[:20])

    def test_parse_instructions_are_a_list_of_strings(self):
        parse = self.cj().parse
        self.assertIsNotNone(parse(reply(instructions=['drop a.py from scope'])))
        for bad in ('move a.py to create', [1], None):
            self.assertIsNone(parse(reply(instructions=bad)), repr(bad))


class TestState(Case):
    def test_state_kind_and_exact_keys(self):
        st = self.cj().build_state('plan', TASK, copy.deepcopy(PLAN), [FINDING])
        self.assertEqual(set(st), STATE_KEYS)
        self.assertEqual((st['kind'], st['check']), ('mr.check.v1', 'plan'))
        self.assertEqual(st['findings'], [FINDING])
        self.assertIn(TASK, st['task'])
        self.assertEqual(st['plan']['requirements'], PLAN['requirements'])
        self.assertEqual(st['plan']['scope']['create'], PLAN['scope']['create'])
        json.dumps(st)

    def test_state_redacts_values_and_keys(self):
        plan = copy.deepcopy(PLAN)
        plan['files_map'][TOKEN] = ['x.py']
        st = self.cj().build_state('plan', 'Call it with ' + TOKEN + ' please.', plan, ['bad ' + TOKEN],
                                   signals={'note': TOKEN})
        text = json.dumps(st)
        self.assertNotIn(TOKEN, text)
        self.assertIn('Call it with', text)

    def test_state_diff_cap_in_scope_first(self):
        other = ('diff --git a/other.py b/other.py\n--- a/other.py\n+++ b/other.py\n@@ -0,0 +1 @@\n'
                 + '+x = 1\n' * 40000)
        mine = ('diff --git a/mul.py b/mul.py\n--- /dev/null\n+++ b/mul.py\n@@ -0,0 +1,2 @@\n'
                '+def mul(a, b):\n+    return a * b\n')
        st = self.cj().build_state('scope', 'Add mul.', copy.deepcopy(PLAN), ['out of scope: other.py'],
                                   signals={'H': 'pass', 'A': 'pass'}, diff=other + mine,
                                   attempt={'rung': 'G-h', 'n': 1})
        diff = st['diff']
        self.assertIsInstance(diff, str)
        self.assertLessEqual(len(diff.encode('utf-8')), 32000)
        self.assertIn('diff --git a/mul.py b/mul.py', diff)
        if 'a/other.py' in diff:
            self.assertLess(diff.index('a/mul.py'), diff.index('a/other.py'))
        self.assertEqual(st['signals'], {'H': 'pass', 'A': 'pass'})
        self.assertEqual(st['attempt'], {'rung': 'G-h', 'n': 1})

    def test_render_uses_template_and_reply_schema(self):
        mod = self.cj()
        self.assertTrue(os.path.isfile(os.path.join(ROOT, 'mr', 'prompts', 'check_judge.v1.txt')))
        text = mod.render(mod.build_state('plan', TASK, copy.deepcopy(PLAN), [FINDING]))
        for part in ('mr.check.v1', TASK, FINDING, 'findings_ok', 'instructions', 'accept', 'repair', 'reject'):
            self.assertIn(part, text, part)


class TestRung(Case):
    def test_plan_rung_default_is_sonnet_high(self):
        mod, cfg = self.cj(), self.cfg()
        for planner, difficulty in (('G-h', 'medium'), ('G-h', 'easy'), ('S', 'medium')):
            got = mod.plan_rung(cfg, planner=planner, difficulty=difficulty, risk='low')
            self.assertEqual(got, 'S', (planner, difficulty))
        self.assertEqual((cfg.rungs['S'].model, cfg.rungs['S'].effort), (SONNET, 'high'))

    def test_plan_rung_strong_for_opus_planner_hard_or_high_risk(self):
        mod, cfg = self.cj(), self.cfg()
        for planner, difficulty, risk in (('O', 'medium', 'low'), ('O+', 'medium', 'low'), ('G-h', 'hard', 'low'),
                                          ('G-h', 'critical', 'low'), ('G-h', 'medium', 'high')):
            got = mod.plan_rung(cfg, planner=planner, difficulty=difficulty, risk=risk)
            self.assertEqual(got, 'S+', (planner, difficulty, risk))
        self.assertEqual((cfg.rungs['S+'].model, cfg.rungs['S+'].effort), (SONNET, 'xhigh'))

    def test_plan_rung_follows_config(self):
        mod = self.cj()
        cfg = self.cfg({'check_judge': {'plan_rung': 'S+'}})
        self.assertEqual(mod.plan_rung(cfg, planner='G-h', difficulty='medium', risk='low'), 'S+')


class TestConfig(Case):
    def test_check_judge_defaults(self):
        block = defaults().get('check_judge')
        self.assertIsInstance(block, dict, 'DEFAULT_YAML needs a check_judge block')
        self.assertEqual({k: block.get(k) for k in WANT}, WANT)
        loaded = self.block(self.cfg())
        self.assertEqual({k: loaded.get(k) for k in WANT}, WANT)

    def test_config_rejects_opus_cursor_or_unknown_rungs(self):
        self.assertEqual(self.block(self.cfg({'check_judge': {'plan_rung': 'S+'}}))['plan_rung'], 'S+')
        for key, code in (('plan_rung', 'O'), ('plan_rung_strong', 'O+'), ('plan_rung', 'G-h'),
                          ('plan_rung_strong', 'C25'), ('plan_rung', 'nope')):
            with self.assertRaises(ConfigError, msg=f'{key}={code}'):
                self.cfg({'check_judge': {key: code}})

    def test_config_rejects_low_effort_sonnet_rung(self):
        low = {'rungs': {'S-m': {'model': SONNET, 'path': 'max', 'effort': 'medium'}}}
        self.block(self.cfg(low))
        with self.assertRaises(ConfigError):
            self.cfg(config._deep_merge(low, {'check_judge': {'plan_rung': 'S-m'}}))

    def test_config_rejects_bad_numbers(self):
        self.block(self.cfg({'check_judge': {'max_plan_calls': 1, 'timeout_s': 120}}))
        for key, val in (('max_plan_calls', 0), ('max_plan_calls', 3), ('max_plan_calls', 'two'),
                         ('timeout_s', 0), ('timeout_s', True)):
            with self.assertRaises(ConfigError, msg=f'{key}={val!r}'):
                self.cfg({'check_judge': {key: val}})


class TestLogSchema(unittest.TestCase):
    def rec(self, **kw):
        rec = {'ts': '2026-11-06T18:00:00+05:30', 'v': 2, 'mr_version': '0.0', 'role': 'check_judge',
               'task': 't-aj8', 'ref': 'cj-t-aj8-1', 'check': 'plan', 'findings': [FINDING],
               'verdict': 'accept', 'reason': 'The gap does not matter.', 'instructions': [], 'rung': 'S',
               'judge_model': SONNET, 'pts_est': 4.0, 'deferred': False, 'status': 'ok'}
        rec.update(kw)
        return rec

    def test_check_judge_record_valid(self):
        self.assertEqual(log_schema_v2.check(self.rec()), [])
        self.assertEqual(log_schema_v2.check(self.rec(verdict=None, reason=None, status='invalid', pts_est=None)), [])
        self.assertEqual(log_schema_v2.check(self.rec(check='scope', verdict='repair',
                                                      instructions=['move a.py to create'])), [])

    def test_check_judge_record_invalid(self):
        self.assertEqual(log_schema_v2.check(self.rec()), [])
        for bad in ({'status': 'weird'}, {'check': 'other'}, {'verdict': 'maybe'}, {'deferred': 'no'},
                    {'findings': 'x'}, {'rung': None}, {'ref': None}):
            self.assertNotEqual(log_schema_v2.check(self.rec(**bad)), [], bad)


if __name__ == '__main__':
    unittest.main()
