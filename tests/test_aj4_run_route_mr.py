'''AJ4 locked tests (CLI): `mr run` and `mr route` with the intake judge on. The merged decision is applied and
logged (route record keys, overrides, task record counts), the fallback chain is cache -> one classifier call ->
rules, `--judge off` is logged, an invariant violation falls back to rules, and `mr route -f FILE` reads the file.
Fake claude and fake cursor-agent only; the judge answer parsing and the classifier are patched.'''
import contextlib
import copy
import io
import json
import os
import unittest
from unittest.mock import patch

from mr import cli, router
from mr import classifier as clf_mod
from mr.judge import client as client_mod
from mr.judge import prompt as prompt_mod
from tests import gitfix
from tests.gitfix import git
from tests.test_run_once import GL, RunCase

HAIKU = 'claude-haiku-5-5'
NL = chr(10)
TEXT = 'fix typo in README.md'
README = {'files': {'README.md': '# test repo' + NL + 'fixed typo' + NL}}
HINTS = ('--type', 'docs', '--difficulty', 'trivial')
OVERRIDE = 'judge override: difficulty trivial -> easy'
ROUTE_KEYS = ('judge', 'hints', 'overrides', 'risk_evidence', 'files', 'judge_absent', 'fallback', 'rules', 'applied')
FIELDS = ('route', 'type', 'difficulty', 'risk')
EASY = {'difficulty': {'decision': 'act', 'value': 1, 'conf': 0.9, 'evidence': 'typo', 'quote_found': True},
        'files': {'change': [], 'new': [], 'complete': None, 'conf': {'change': None, 'new': None, 'complete': None}},
        'tags': {}}


def clf_out(difficulty, risk):
    return clf_mod.ClassifierOut(
        task_type='docs', difficulty=difficulty, risk=risk, risk_tags=(), load_bearing=False, high_judgment=False,
        scope_files_est=1, ambiguity='low', novelty='boilerplate', context='small', model=None, effort=None,
        needs_worktree=False, needs_review=False, needs_clarification=False, confidence=0.95, reason='fake')


class FakeClassifier:
    '''Stands in for mr.classifier.classify and counts the calls.'''

    def __init__(self, out=None, status='ok'):
        self.out, self.status, self.calls = out, status, 0

    def __call__(self, task, features, cfg, *args, **kw):
        self.calls += 1
        ref = kw.get('ref') or 'c-%s-1' % task.id
        return clf_mod.ClassifyResult(self.out, self.status, 1 if self.out else 0, 5, '0' * 12, ref, {},
                                      'grok-4.7-low')


class OnCase(RunCase):
    clf = None

    def setUp(self):
        super().setUp()
        self.scene(1)

    def scene(self, runs):
        self.set_scenario({'cursor': {GL: [README] * runs}, 'claude': {HAIKU: [{'result': '{}'}] * (2 * runs)}})
        self.env['MR_HOME'] = self.home

    def write_cfg(self):
        raw = {'paths': {'home': self.home, 'cursor_agent': gitfix.FAKE_CURSOR, 'claude': gitfix.FAKE_CLAUDE,
                         'check': gitfix.FAKE_CHECK}, 'judge': {'mode': 'on'}}
        if self.clf:
            raw['classifier'] = dict(self.clf)
        with open(self.cfgpath, 'w', encoding='utf-8') as fh:
            json.dump(raw, fh)

    def judge_says(self, reading=EASY):
        stack = contextlib.ExitStack()
        stack.enter_context(patch('mr.judge.prompt.parse_answers_v2',
                                  lambda *a, **k: prompt_mod.ParsedAnswers('ok', {}, ())))
        stack.enter_context(patch('mr.judge.policy.read_route_v2', lambda *a, **k: copy.deepcopy(reading)))
        return stack

    def judge_fails(self, status='timeout'):
        return patch('mr.judge.client.call', lambda *a, **k: client_mod.RawResult(status, latency_ms=12000))

    def with_clf(self, fake):
        return patch('mr.classifier.classify', new=fake)

    def judge_calls(self):
        return [c for c in self.calls() if c.get('cli') == 'claude' and c.get('model') == HAIKU]

    def route_rec(self):
        recs = self.records('route')
        self.assertEqual(len(recs), 1, recs)
        return recs[0]

    def reset_repo(self):
        git(self.repo, 'checkout', '-q', '-f', self.branch)
        git(self.repo, 'reset', '-q', '--hard')
        git(self.repo, 'clean', '-q', '-f', '-d')

    def route(self, *extra, json_out=False):
        argv = ['--config', self.cfgpath] + (['--json'] if json_out else []) + ['route', *extra, '--repo', self.repo]
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


class RunOnTest(OnCase):
    def test_run_judge_ok_applies_the_merge_and_logs_it(self):
        with self.judge_says():
            code, _, err = self.mr(*HINTS, '--latency', 'fast')
        self.assertIn(OVERRIDE, err)
        self.assertEqual(code, 0, err)
        route = self.route_rec()
        for key in ROUTE_KEYS:
            self.assertIn(key, route, key)
        self.assertEqual((route['rules']['difficulty'], route['applied']['difficulty']), ('trivial', 'easy'))
        self.assertEqual(route['applied']['ladder'], ['G-l', 'G-m'])
        self.assertEqual(((route['judge'] or {}).get('status'), route['judge_absent'], route['fallback']),
                         ('ok', False, None))
        self.assertEqual((route['hints'].get('type'), route['hints'].get('difficulty')), ('docs', 'trivial'))
        got = [(o['field'], o['hint'], o['applied'], o['judge_conf']) for o in route['overrides']]
        self.assertEqual(got, [('difficulty', 'trivial', 'easy', 0.9)])
        task = self.only_task()
        self.assertEqual((task['difficulty'], task['judge_overrides']), ('easy', 1))
        self.assertIsInstance(task['files_src'], dict)
        judged = self.records('judge')
        self.assertEqual([(r['mode'], r['status']) for r in judged], [('on', 'ok')])
        self.assertIn('difficulty', judged[0]['applied'])
        self.assertEqual(route['judge']['ref'], judged[0]['ref'])
        self.assertEqual(len(self.judge_calls()), 1)
        self.assert_schema()

    def test_run_judge_timeout_falls_back_to_one_classifier_call(self):
        fake = FakeClassifier(clf_out('trivial', 'medium'))
        with self.judge_fails('timeout'), self.with_clf(fake):
            code, _, err = self.mr('--type', 'docs', '--difficulty', 'easy')
        self.assertEqual(fake.calls, 1, err)
        self.assertEqual(code, 0, err)
        route = self.route_rec()
        self.assertEqual(((route.get('judge') or {}).get('status'), route.get('fallback'), route.get('judge_absent')),
                         ('timeout', 'classifier', False))
        self.assertEqual((route['applied']['difficulty'], route['applied']['risk']), ('easy', 'medium'))
        self.assertEqual([(r['mode'], r['status']) for r in self.records('judge')], [('on', 'timeout')])
        self.assertEqual(self.judge_calls(), [])

    def test_run_without_judge_or_classifier_runs_on_rules(self):
        fake = FakeClassifier(None, 'cli_unavailable')
        with self.judge_fails('timeout'), self.with_clf(fake):
            code, _, err = self.mr(*HINTS)
        self.assertEqual(fake.calls, 1, err)
        self.assertEqual(code, 0, err)
        route = self.route_rec()
        self.assertEqual((route.get('fallback'), route.get('judge_absent')), ('rules', True))
        self.assertEqual({k: route['applied'][k] for k in FIELDS}, {k: route['rules'][k] for k in FIELDS})
        self.assertEqual(route['applied']['ladder'], ['G-l'])
        self.assertEqual(self.only_task()['outcome'], 'accepted')

    def test_classifier_off_flag_skips_the_fallback_call(self):
        fake = FakeClassifier(clf_out('critical', 'high'))
        with self.judge_fails('cli_error'), self.with_clf(fake):
            code, _, err = self.mr(*HINTS, '--classifier', 'off')
        route = self.route_rec()
        self.assertEqual((route.get('fallback'), route.get('judge_absent')), ('rules', True), err)
        self.assertEqual(fake.calls, 0)
        self.assertEqual(code, 0, err)
        self.assertEqual(route['applied']['difficulty'], 'trivial')

    def test_breaker_open_and_budget_denied_fall_back(self):
        self.scene(2)
        cases = (('breaker_open', patch('mr.judge.breaker.allow', return_value=False)),
                 ('budget_denied', patch('mr.judge.budget.charge', return_value=(False, 'budget_denied', {}))))
        for status, blocker in cases:
            fake = FakeClassifier(clf_out('trivial', 'low'))
            start = len(self.log())
            with blocker, self.with_clf(fake):
                code, _, err = self.mr(*HINTS)
            self.reset_repo()
            route = [r for r in self.log()[start:] if r['role'] == 'route']
            self.assertEqual(len(route), 1, (status, err))
            self.assertEqual(((route[0].get('judge') or {}).get('status'), route[0].get('fallback')),
                             (status, 'classifier'), err)
            self.assertEqual((fake.calls, code), (1, 0), (status, err))
        self.assertEqual(self.judge_calls(), [])

    def test_judge_off_flag_is_rules_only_and_logged(self):
        with self.judge_says():
            code, _, err = self.mr(*HINTS, '--judge', 'off')
        self.assertIn('judge off by flag (debug)', err)
        route = self.route_rec()
        self.assertIs(route.get('judge_off_by_flag'), True)
        self.assertEqual(code, 0, err)
        self.assertEqual(route['applied']['difficulty'], 'trivial')
        self.assertEqual(route['applied']['ladder'], ['G-l'])
        self.assertIsNone(route['judge'])
        self.assertEqual(self.records('judge'), [])
        self.assertEqual(self.judge_calls(), [])
        self.assertNotIn('judge override', err)

    def test_invariant_violation_falls_back_to_rules_and_runs(self):
        with self.judge_says(), patch('mr.judge.merge.merge', side_effect=router.InvariantError('I1 prime')):
            code, _, err = self.mr(*HINTS)
        route = self.route_rec()
        self.assertIs(route['invariant_violation'], True, err)
        self.assertEqual(code, 0, err)
        self.assertEqual(route['applied']['difficulty'], 'trivial')
        self.assertEqual([r['status'] for r in self.records('judge')], ['invariant_violation'])
        self.assertEqual(self.only_task()['outcome'], 'accepted')

    def test_judge_on_skips_the_background_classifier_shadow(self):
        self.clf = {'mode': 'shadow', 'sample_n': 1}
        self.write_cfg()
        fake = FakeClassifier(clf_out('critical', 'high'))
        with self.judge_says(), self.with_clf(fake):
            code, _, err = self.mr(text=TEXT)
        self.assertEqual(fake.calls, 0, err)
        self.assertEqual(code, 0, err)
        self.assertEqual([r['status'] for r in self.records('judge')], ['ok'])


class RouteOnTest(OnCase):
    def test_route_prints_overrides_and_json_has_judge(self):
        with self.judge_says():
            code, out, err = self.route(TEXT, *HINTS)
            _jcode, jout, jerr = self.route(TEXT, *HINTS, json_out=True)
        self.assertIn(OVERRIDE, out + err)
        self.assertIn('difficulty: easy', out)
        self.assertEqual(code, 0, err)
        doc = json.loads(jout)
        self.assertIn((doc.get('judge') or {}).get('status'), ('ok', 'cache_hit'), jout + jerr)
        self.assertEqual([(o['field'], o['applied']) for o in doc.get('overrides') or []], [('difficulty', 'easy')])
        self.assertEqual(doc['decision']['difficulty'], 'easy')
        self.assertEqual(self.log(), [])

    def test_route_reads_the_task_text_from_a_file(self):
        path = os.path.join(self.dir, 'task.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'text': TEXT}, fh)
        code, out, err = self.route('-f', path, '--no-models')
        self.assertNotIn('task text missing', err)
        self.assertEqual(code, 0, err)
        self.assertIn('task_id: t-', out)
        self.assertIn('route: cascade', out)


if __name__ == '__main__':
    unittest.main()
