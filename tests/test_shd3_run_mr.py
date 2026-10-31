'''SHD3 locked tests: classifier shadow wiring in `mr run` (gate, background thread, join at task end, records).'''
import json
import os
import random
import sys
import threading
import unittest
from unittest.mock import patch

from mr import classifier as clf_mod
from mr import runners, shadow
from mr.task import Params, Task, fingerprint
from tests import gitfix
from tests.gitfix import git
from tests.test_loop_run import GL, LoopCase
from tests.test_run_once import RunCase, _wait

UNSURE = 'maybe fix typo in README.md or something?'
README = {'files': {'README.md': 'fixed typo'}}
GIVEN = ('--type', 'docs', '--difficulty', 'trivial', '--route', 'cascade')
VOLATILE = {'ts', 'task', 'started_at', 'ended_at', 'wall_s', 'ttg_s', 'raw_dir', 'cost'}
ROUTE_KEYS = ('fingerprint', 'prepass_confident', 'rules', 'applied', 'sources', 'invariant_violation')
TASK_KEYS = ('type', 'difficulty', 'risk', 'risk_tags', 'route', 'route_src', 'ladder', 'first_rung', 'final_rung',
             'esc', 'outcome', 'exit', 'final_signals', 'files_n')
VERBS = ('fix', 'update', 'refactor', 'document', 'add a test for', 'maybe rework')
OBJECTS = ('the typo in README.md', 'the login check in src/auth.ts', 'the payment retry in src/pay.ts',
           'the db migration in db/001.sql', 'the helper in src/util.py')
TAILS = ('', ' please', '?', ' if that is safe?')
TYPES = (None, 'docs', 'bugfix', 'code', 'refactor', 'test')
DIFFS = (None, 'trivial', 'easy', 'medium', 'hard', 'critical')
ROUTES = (None, 'cascade', 'judgment', 'design')


def _out(task_type):
    return clf_mod.ClassifierOut(
        task_type=task_type, difficulty='critical', risk='high', risk_tags=('payments',), load_bearing=True,
        high_judgment=True, scope_files_est=40, ambiguity='high', novelty='novel', context='large', model='O',
        effort='xhigh', needs_worktree=True, needs_review=True, needs_clarification=True, confidence=1.0,
        reason='adversarial fake')


class FakeClassifier:
    '''Stands in for mr.classifier.classify: an adversarial answer, or an exception.'''

    def __init__(self, calls=1, boom=False):
        self.n, self.boom = calls, boom
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, task, features, cfg, *args, **kw):
        ref = kw.get('ref', args[0] if args else None)
        with self.lock:
            self.calls.append((task.id, ref))
        if self.boom:
            raise RuntimeError('fake classifier crashed')
        other = 'ios-native' if features.type != 'ios-native' else 'docs'
        return clf_mod.ClassifyResult(out=_out(other), status='ok', calls=self.n, latency_ms=5,
                                      prompt_sha='0' * 12, ref=ref, redactions={}, model='grok-4.7-low')


def _strip(rec):
    return {k: v for k, v in rec.items() if k not in VOLATILE}


def _decision(recs):
    route = [r for r in recs if r['role'] == 'route']
    task = [r for r in recs if r['role'] == 'task']
    impl = [(r['rung'], r.get('failure_class')) for r in recs if r['role'] == 'implementer']
    return ({k: route[0].get(k) for k in ROUTE_KEYS} if route else None,
            {k: task[0].get(k) for k in TASK_KEYS} if task else None, impl)


class _ShadowCase(RunCase):
    clf = None

    def set_scenario(self, scenario, check=None):
        super().set_scenario(scenario, check)
        self.env['MR_HOME'] = self.home

    def write_cfg(self):
        raw = {'paths': {'home': self.home, 'cursor_agent': gitfix.FAKE_CURSOR,
                         'claude': gitfix.FAKE_CLAUDE, 'check': gitfix.FAKE_CHECK}}
        if self.clf:
            raw['classifier'] = dict(self.clf)
        with open(self.cfgpath, 'w', encoding='utf-8') as fh:
            json.dump(raw, fh)

    def set_clf(self, **kw):
        self.clf = kw
        self.write_cfg()

    def run_with(self, fake, *extra, **kw):
        with patch('mr.classifier.classify', new=fake):
            return self.mr(*extra, **kw)

    def reset_repo(self):
        git(self.repo, 'checkout', '-q', '-f', self.branch)
        git(self.repo, 'reset', '-q', '--hard')
        git(self.repo, 'clean', '-q', '-f', '-d')

    def find_text(self, sampled):
        '''A task text whose fingerprint is (or is not) in the default 1-in-3 sample.'''
        base = gitfix.head(self.repo)
        params = Params(type='docs', difficulty='trivial', route='cascade')
        for i in range(500):
            text = f'fix typo in README.md, case {i}'
            task = Task(id='t-00000000', text=text, repo=self.repo, base=base, params=params,
                        files_named=('README.md',), source='cli')
            if shadow.should_run(params, True, fingerprint(task), 3)[0] == sampled:
                return text
        self.fail('no fingerprint found')


class ShadowRunTest(_ShadowCase):
    def test_off_records_unchanged_and_shadow_runs(self):
        fake = FakeClassifier()
        self.cursor(README, README, README)
        snaps = []
        for extra in ((), ('--classifier', 'off')):
            start = len(self.log())
            code, _, err = self.run_with(fake, *extra, text=UNSURE)
            recs = self.log()[start:]
            route = [r for r in recs if r['role'] == 'route']
            task = [r for r in recs if r['role'] == 'task']
            self.assertEqual((len(route), len(task)), (1, 1), err)
            self.assertEqual([r for r in recs if r['role'] == 'classifier'], [])
            self.assertIsNone(route[0]['judge'])
            self.assertIsNone(route[0]['classifier'])
            self.assertEqual(task[0]['shadow'], {'judge': None, 'classifier': None})
            self.assertEqual(task[0]['attempts']['classifier'], 0)
            snaps.append((code, _strip(route[0]), _strip(task[0])))
            self.reset_repo()
        self.assertEqual(snaps[0], snaps[1])
        self.assertEqual(fake.calls, [])
        self.assertEqual(len(self.calls()), 2)  # the two rung calls only: no classifier subprocess
        code, _, err = self.run_with(fake, '--classifier', 'shadow', text=UNSURE)
        self.assertEqual(len(fake.calls), 1, err)
        self.assertEqual(code, snaps[0][0])

    def test_unsure_task_runs_classifier(self):
        fake = FakeClassifier(calls=2)
        self.cursor(README)
        code, _, err = self.run_with(fake, '--classifier', 'shadow', text=UNSURE)
        clf = self.records('classifier')
        self.assertEqual(len(clf), 1, err)
        self.assertEqual(len(fake.calls), 1)
        task = self.only_task()
        tid = task['task']
        ref = f'c-{tid}-1'
        self.assertEqual(fake.calls[0], (tid, ref))
        rec = clf[0]
        self.assertEqual((rec['status'], rec['consulted'], rec['calls'], rec['ref'], rec['mode']),
                         ('ok', True, 2, ref, 'shadow'))
        self.assertEqual(rec['answers']['difficulty'], 'critical')
        self.assertEqual(task['shadow']['classifier'], {'route': 'design'})
        self.assertIsNone(task['shadow']['judge'])
        self.assertEqual(task['attempts']['classifier'], 2)
        routes = self.records('route')
        self.assertEqual(len(routes), 1)
        summ = routes[0]['classifier']
        self.assertIsInstance(summ, dict)
        self.assertEqual((summ['status'], summ['ref'], summ['consulted'], summ['route'], summ['type']),
                         ('pending', ref, True, None, None))
        self.assertEqual(routes[0]['applied']['route'], routes[0]['rules']['route'])
        self.assert_schema()

    def test_confident_unsampled_task_is_skipped(self):
        text = self.find_text(False)
        fake = FakeClassifier()
        self.cursor(README)
        code, _, err = self.run_with(fake, '--classifier', 'shadow', *GIVEN, text=text)
        clf = self.records('classifier')
        self.assertEqual(len(clf), 1, err)
        self.assertEqual(fake.calls, [])
        self.assertEqual((clf[0]['status'], clf[0]['calls'], clf[0]['consulted']), ('skipped_confident', 0, False))
        routes = self.records('route')
        self.assertEqual(len(routes), 1)
        summ = routes[0]['classifier']
        self.assertIsInstance(summ, dict)
        self.assertEqual((summ['status'], summ['consulted']), ('skipped_confident', False))
        task = self.only_task()
        self.assertIsNone(task['shadow']['classifier'])
        self.assertEqual(task['attempts']['classifier'], 0)
        self.assert_schema()

    def test_confident_sampled_task_runs_unconsulted(self):
        text = self.find_text(True)
        fake = FakeClassifier()
        self.cursor(README)
        code, _, err = self.run_with(fake, '--classifier', 'shadow', *GIVEN, text=text)
        clf = self.records('classifier')
        self.assertEqual(len(clf), 1, err)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual((clf[0]['status'], clf[0]['consulted'], clf[0]['calls']), ('ok', False, 1))
        routes = self.records('route')
        self.assertEqual(len(routes), 1)
        summ = routes[0]['classifier']
        self.assertIsInstance(summ, dict)
        self.assertEqual((summ['status'], summ['consulted']), ('pending', False))
        task = self.only_task()
        self.assertEqual(task['shadow']['classifier'], {'route': 'design'})
        self.assertEqual(task['attempts']['classifier'], 1)
        self.assert_schema()

    def test_shadow_never_changes_decisions(self):
        self.set_clf(sample_n=1)
        self.cursor(*([README] * 45))
        fake = FakeClassifier()
        ran = 0
        for seed in range(20):
            rnd = random.Random(seed)
            text = f'{rnd.choice(VERBS)} {rnd.choice(OBJECTS)}{rnd.choice(TAILS)}'
            extra = []
            for flag, vals in (('--type', TYPES), ('--difficulty', DIFFS), ('--route', ROUTES)):
                val = rnd.choice(vals)
                if val:
                    extra += [flag, val]
            got = {}
            for mode in ('off', 'shadow'):
                start = len(self.log())
                code, _, err = self.run_with(fake, '--classifier', mode, *extra, text=text)
                recs = self.log()[start:]
                self.reset_repo()
                got[mode] = (code, _decision(recs))
                route = [r for r in recs if r['role'] == 'route']
                clf = [r for r in recs if r['role'] == 'classifier']
                if mode == 'off' or not route:
                    self.assertEqual(clf, [], (seed, mode, text, extra))
                    continue
                self.assertEqual(len(clf), 1, (seed, text, extra, err))
                self.assertEqual(clf[0]['status'], 'ok')
                applied = route[0]['applied']
                self.assertEqual({k: v for k, v in applied.items() if k != 'ladder'}, route[0]['rules'])
                ran += 1
            self.assertEqual(got['off'], got['shadow'], (seed, text, extra))
        self.assertEqual(len(fake.calls), ran)
        self.assertGreaterEqual(ran, 10)

    def test_join_timeout_logs_timeout_and_kills_child(self):
        self.set_clf(sample_n=1, join_timeout_s=0.3)
        self.cursor(README)
        done = threading.Event()
        pidfile = os.path.join(self.dir, 'clf-child.pid')
        child = ('import os, sys, time, pathlib; '
                 'pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)')
        late = FakeClassifier()

        def slow(task, features, cfg, *args, **kw):
            env = {k: v for k, v in os.environ.items() if k != 'MR_TEST_TIME_SCALE'}
            try:
                runners.exec_child([sys.executable, '-c', child, pidfile], self.dir, env, 120,
                                   os.path.join(self.dir, 'clf-raw', 'classifier-1'))
            finally:
                done.set()
            return late(task, features, cfg, *args, **kw)

        code, _, err = self.run_with(slow, '--classifier', 'shadow')
        self.assertEqual(code, 0, err)
        clf = self.records('classifier')
        self.assertEqual(len(clf), 1, err)
        tid = self.only_task()['task']
        self.assertEqual((clf[0]['status'], clf[0]['calls'], clf[0]['ref']), ('timeout', 1, f'c-{tid}-1'))
        self.assertTrue(done.wait(15), 'the classifier child was not killed at task end')
        if os.path.isfile(pidfile):
            with open(pidfile, encoding='utf-8') as fh:
                pid = int(fh.read().strip() or 0)
            if pid:
                self.assertTrue(_wait(lambda: not os.path.exists(f'/proc/{pid}'), 5), pid)
        self.assertEqual(len(self.records('classifier')), 1)  # a late answer is never logged
        self.assertIsNone(self.only_task()['shadow']['classifier'])

    def test_classifier_exception_logs_cli_error(self):
        self.set_clf(sample_n=1)
        self.cursor(README)
        fake = FakeClassifier(boom=True)
        code, _, err = self.run_with(fake, '--classifier', 'shadow')
        self.assertEqual(code, 0, err)
        clf = self.records('classifier')
        self.assertEqual(len(clf), 1, err)
        self.assertEqual((clf[0]['status'], clf[0]['mode']), ('cli_error', 'shadow'))
        self.assertEqual(len(fake.calls), 1)
        self.assertIsNone(self.only_task()['shadow']['classifier'])
        self.assert_schema()


class ShadowLoopTest(LoopCase):
    def set_scenario(self, scenario, check=None):
        super().set_scenario(scenario, check)
        self.env['MR_HOME'] = self.home

    def write_cfg(self):
        raw = {'paths': {'home': self.home, 'cursor_agent': gitfix.FAKE_CURSOR,
                         'claude': gitfix.FAKE_CLAUDE, 'check': gitfix.FAKE_CHECK},
               'hygiene': {'commands': {'repo': self.hygiene_cmds}},
               'acceptance': {'runners': {'repo': 'unittest'}},
               'classifier': {'sample_n': 1}}
        with open(self.cfgpath, 'w', encoding='utf-8') as fh:
            json.dump(raw, fh)

    def test_full_ladder_runs_classifier(self):
        self.cursor(README, model=GL)
        fake = FakeClassifier()
        with patch('mr.classifier.classify', new=fake):
            code, _, err = self.run_json('--type', 'docs', '--difficulty', 'trivial', '--classifier', 'shadow')
        self.assertEqual(code, 0, err)
        clf = self.records('classifier')
        self.assertEqual(len(clf), 1, err)
        self.assertEqual(clf[0]['status'], 'ok')
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.models(), [GL])
        task = self.only_task()
        self.assertEqual(task['shadow']['classifier'], {'route': 'design'})
        self.assertEqual(task['attempts']['classifier'], 1)
        routes = self.records('route')
        self.assertEqual(len(routes), 1)
        self.assertEqual((routes[0]['classifier'] or {}).get('status'), 'pending')
        self.assert_schema()


if __name__ == '__main__':
    unittest.main()
