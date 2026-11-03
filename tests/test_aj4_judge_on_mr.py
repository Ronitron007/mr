'''AJ4 locked tests (unit): judge.mode is on by default, judge mode `on` no longer runs as shadow, and the Judge
facade in mode on: decide_route (synchronous), a cache hit counts as the judge, at most 3 calls per task, and
note_applied writes the one judge record. Fake claude through exec_fn only; answer parsing patched to a fixed reading.'''
import contextlib
import copy
import importlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from mr import config, prepass, router, shadow
from mr.defaults import defaults
from mr.facts import Candidate, Facts
from mr.judge import prompt as prompt_mod
from mr.log import Log
from mr.task import Params, make_task
from tests import gitfix
from tests.test_run_once import CHECK_DEFAULT

HAIKU = 'claude-haiku-5-5'
NL = chr(10)
TID = 't-0000a4c1'
TEXT = 'fix typo in README.md'
FACTS = Facts(path_tokens=('README.md',), paths_exist={'README.md': True},
              candidates=(Candidate('README.md', True, ('named', 'exists')),), risk_evidence=(), sensitive_paths=(),
              type_features={'lead_verb': 'fix', 'test_only_scope': False, 'keyword_hits': {}}, repo_files=1)
READING = {'difficulty': {'decision': 'act', 'value': 1, 'conf': 0.9, 'evidence': 'typo', 'quote_found': True},
           'files': {'change': [], 'new': [], 'complete': None,
                     'conf': {'change': None, 'new': None, 'complete': None}},
           'tags': {}}


def envelope(result):
    return json.dumps({
        'type': 'result', 'subtype': 'success', 'is_error': False, 'result': result, 'duration_ms': 900,
        'duration_api_ms': 700, 'total_cost_usd': 0.0012, 'usage': {'input_tokens': 900, 'output_tokens': 120},
        'modelUsage': {HAIKU: {'inputTokens': 900, 'outputTokens': 120}}})


class Recorder:
    '''Stands in for runners.exec_child: (argv, cwd, env, timeout_s, raw_base) -> (rc, out, err, wall_s, timed_out).'''

    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd, env, timeout_s, raw_base):
        self.calls.append(list(argv))
        return 0, envelope('{}'), '', 0.05, False


class OnCase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = os.path.realpath(td.name)
        self.home = os.path.join(self.dir, 'home')
        self.repo = os.path.join(self.dir, 'repo')
        os.makedirs(self.home)
        os.makedirs(self.repo)
        self.wrapper = os.path.join(self.dir, 'bin', 'claude')
        os.makedirs(os.path.dirname(self.wrapper))
        with open(self.wrapper, 'w', encoding='utf-8') as fh:
            fh.write('#!/bin/sh' + NL + 'exit 0' + NL)
        os.chmod(self.wrapper, 0o755)
        check = gitfix.write_check(self.dir, {'default': dict(CHECK_DEFAULT)})
        patches = (
            mock.patch.dict(os.environ, {'MR_HOME': self.home, 'MR_FAKE_CHECK': check, 'MR_TEST_TIME_SCALE': '1'}),
            mock.patch('mr.judge.prompt.parse_answers_v2', lambda *a, **k: prompt_mod.ParsedAnswers('ok', {}, ())),
            mock.patch('mr.judge.policy.read_route_v2', lambda *a, **k: copy.deepcopy(READING)),
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def cfg(self, **judge):
        over = {'paths': {'home': self.home, 'check': gitfix.FAKE_CHECK, 'claude': self.wrapper}}
        if judge:
            over['judge'] = judge
        return config.from_dict(config._deep_merge(defaults(), over))

    def new_judge(self, rec=None, task_id=TID):
        mod = importlib.import_module('mr.judge.judge')
        return mod.Judge(self.cfg(), 'on', Log(self.home), task_id=task_id, exec_fn=rec)

    def decide(self, judge, text=TEXT):
        self.assertTrue(callable(getattr(judge, 'decide_route', None)),
                        'Judge.decide_route(task, features, facts, rules) is missing')
        task = make_task(text, self.repo, '0' * 40, Params(type='docs', difficulty='trivial'))
        feats = prepass.extract(task, None)
        return judge.decide_route(task, feats, FACTS, router.decide(task, feats, self.cfg()))

    def records(self):
        path = os.path.join(self.home, 'log', 'runs.jsonl')
        if not os.path.isfile(path):
            return []
        with open(path, encoding='utf-8') as fh:
            recs = [json.loads(ln) for ln in fh if ln.strip()]
        return [r for r in recs if r.get('role') == 'judge']


class ModeTest(OnCase):
    def test_judge_mode_on_by_default_and_on_budget_key(self):
        raw = defaults()
        self.assertEqual(raw['judge']['mode'], 'on')
        self.assertEqual(raw['judge']['repos'], 'all')
        self.assertEqual(self.cfg().judge['mode'], 'on')
        self.assertEqual(raw['judge']['budget'].get('max_calls_per_task_on'), 3)
        self.assertEqual(raw['judge']['budget'].get('max_calls_per_task'), 6)
        for bad in (0, -1, 'x', True, 2.5):
            with self.assertRaises(config.ConfigError, msg=repr(bad)):
                self.cfg(budget={'max_calls_per_task_on': bad})

    def test_judge_mode_on_is_on_without_not_built_line(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = (shadow.judge_mode('on', {}), shadow.judge_mode(None, {'mode': 'on'}),
                   shadow.judge_mode('off', {'mode': 'on'}), shadow.judge_mode('shadow', {'mode': 'on'}))
        self.assertEqual(got, ('on', 'on', 'off', 'shadow'))
        self.assertNotIn('not built', err.getvalue())
        self.assertEqual(self.new_judge().mode, 'on')


class DecideRouteTest(OnCase):
    def test_decide_route_ok_returns_judge_readings(self):
        rec = Recorder()
        judge = self.new_judge(rec)
        out = self.decide(judge)
        self.assertEqual((out.status, out.source), ('ok', 'judge'))
        self.assertEqual(out.readings['difficulty']['value'], 1)
        self.assertEqual(out.ref, 'j-%s-1' % TID)
        self.assertIsInstance(out.latency_ms, int)
        self.assertEqual(len(rec.calls), 1)
        self.assertIn(HAIKU, rec.calls[0])
        self.assertEqual(judge.mode, 'on')

    def test_cache_hit_counts_as_the_judge_without_a_call(self):
        rec = Recorder()
        first = self.decide(self.new_judge(rec, task_id='t-0000a4a1'))
        second = self.decide(self.new_judge(rec, task_id='t-0000a4a2'))
        self.assertEqual(first.status, 'ok')
        self.assertEqual((second.status, second.source), ('cache_hit', 'cache'))
        self.assertEqual(second.readings['difficulty']['value'], 1)
        self.assertEqual(len(rec.calls), 1)

    def test_on_mode_makes_at_most_three_calls_per_task(self):
        rec = Recorder()
        judge = self.new_judge(rec)
        outs = [self.decide(judge, text=f'fix typo number {i} in README.md') for i in range(4)]
        self.assertEqual([o.status for o in outs], ['ok', 'ok', 'ok', 'budget_denied'])
        self.assertIsNone(outs[3].readings)
        self.assertEqual(len(rec.calls), 3)

    def test_note_applied_writes_one_on_record(self):
        rec = Recorder()
        judge = self.new_judge(rec)
        out = self.decide(judge)
        self.assertTrue(callable(getattr(judge, 'note_applied', None)), 'Judge.note_applied is missing')
        judge.note_applied(out.ref, ['difficulty'], ())
        recs = self.records()
        self.assertEqual(len(recs), 1, recs)
        r = recs[0]
        self.assertEqual((r['ref'], r['mode'], r['status'], r['applied']), (out.ref, 'on', 'ok', ['difficulty']))
        self.assertEqual(r['qset'], 'route.v2')


if __name__ == '__main__':
    unittest.main()
