'''SHD7 locked tests: the Judge facade (one shadow route call, its record and statuses), the judge CLI check in
guard, the judge config keys, and AC-JDG-2 at decide() level (an adversarial judge never changes a decision).'''
import importlib
import json
import os
import random
import tempfile
import threading
import time
import unittest
from unittest import mock

from mr import config, guard, prepass, router
from mr.defaults import defaults
from mr.judge import prompt as prompt_mod
from mr.judge import questions as qmod
from mr.log import Log
from mr.task import Params, make_task
from tests import gitfix
from tests.test_run_once import CHECK_DEFAULT

HAIKU = 'claude-haiku-5-5'
NL = chr(10)
TID = 't-0000d7a1'
UNSURE = 'maybe fix typo in README.md or something?'
CANARY = 'purple' + 'wombat'
ENV_CANARY = 'teal' + 'aardvark'
RECORD_KEYS = ('ref', 'use', 'qset', 'qset_sha', 'mode', 'backend', 'effort', 'model_req', 'model_served', 'status',
               'exit_code', 'latency_ms', 'duration_api_ms', 'prompt_sha', 'state_sha', 'state_bytes',
               'state_tokens_est', 'input_tokens', 'output_tokens', 'usd_equiv', 'pts_est', 'redactions',
               'truncated', 'egress', 'answers', 'decisions', 'applied', 'overridden_by', 'budget_after',
               'consulted')
VERBS = ('fix', 'update', 'refactor', 'document', 'add a test for', 'maybe rework')
OBJECTS = ('the typo in README.md', 'the login check in src/auth.ts', 'the payment retry in src/pay.ts',
           'the db migration in db/001.sql', 'the helper in src/util.py')
TAILS = ('', ' please', '?', ' if that is safe?')
TYPES = (None, 'docs', 'bugfix', 'code', 'refactor', 'test')
DIFFS = (None, 'trivial', 'easy', 'medium', 'hard', 'critical')
ROUTES = (None, 'cascade', 'judgment', 'design')


def answers_json():
    '''Adversarial judge: route design, type docs, every score at its top, every yes/no yes, confidence 1.0.'''
    out = {}
    for i, (qid, q) in enumerate(qmod.load('route.v1').items(), 1):
        if qid == 'route_class':
            ans = 'design'
        elif qid == 'task_type':
            ans = 'docs'
        elif q.get('type') == 'choice':
            ans = list(q['criteria'])[-1]
        elif q.get('type') == 'score':
            ans = len(q['criteria']) - 1
        else:
            ans = True
        out['q%02d' % i] = {'answer': ans, 'confidence': 1.0}
    return json.dumps({'answers': out})


def envelope(result):
    return json.dumps({
        'type': 'result', 'subtype': 'success', 'is_error': False, 'result': result, 'duration_ms': 900,
        'duration_api_ms': 700, 'total_cost_usd': 0.0012, 'usage': {'input_tokens': 900, 'output_tokens': 120},
        'modelUsage': {HAIKU: {'inputTokens': 900, 'outputTokens': 120}}})


def _alive(pid):
    try:
        with open(f'/proc/{pid}/stat', encoding='utf-8') as fh:
            state = fh.read().rsplit(')', 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state not in ('Z', 'X')


def _wait(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class Recorder:
    '''Stands in for runners.exec_child: (argv, cwd, env, timeout_s, raw_base) -> (rc, out, err, wall_s, timed_out).'''

    def __init__(self, rc=0, out=None, gate=None):
        self.calls = []
        self.lock = threading.Lock()
        self.gate = gate
        self.reply = (rc, envelope(answers_json()) if out is None else out, '', 0.05, False)

    def __call__(self, argv, cwd, env, timeout_s, raw_base):
        with self.lock:
            self.calls.append(list(argv))
        if self.gate is not None:
            self.gate.wait(10)
        return self.reply


class JudgeCase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = os.path.realpath(td.name)
        self.home = os.path.join(self.dir, 'home')
        self.repo = os.path.join(self.dir, 'repo')
        os.makedirs(self.home)
        os.makedirs(self.repo)
        self.wrapper = self.write_wrapper('claude', ['#!/bin/sh', 'exit 0'])
        check = gitfix.write_check(self.dir, {'default': dict(CHECK_DEFAULT)})
        patcher = mock.patch.dict(os.environ, {'MR_HOME': self.home, 'MR_FAKE_CHECK': check,
                                               'MR_TEST_TIME_SCALE': '1', 'MR_SHD7_CANARY': ENV_CANARY})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_wrapper(self, name, lines, mode=0o755):
        path = os.path.join(self.dir, 'bin', name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(NL.join(lines) + NL)
        os.chmod(path, mode)
        return path

    def cfg(self, claude=None, **judge):
        over = {'paths': {'home': self.home, 'check': gitfix.FAKE_CHECK, 'claude': claude or self.wrapper}}
        if judge:
            over['judge'] = judge
        return config.from_dict(config._deep_merge(defaults(), over))

    def mod(self):
        try:
            return importlib.import_module('mr.judge.judge')
        except ModuleNotFoundError as exc:
            self.fail(f'mr.judge.judge is missing: {exc}')

    def make(self, text=UNSURE):
        task = make_task(text, self.repo, '0' * 40, Params())
        return task, prepass.extract(task, None)

    def new_judge(self, cfg, exec_fn=None, task_id=TID):
        return self.mod().Judge(cfg, 'shadow', Log(self.home), task_id=task_id, exec_fn=exec_fn)

    def ask(self, cfg, exec_fn, text=UNSURE, task_id=TID):
        judge = self.new_judge(cfg, exec_fn, task_id)
        task, feats = self.make(text)
        ref = judge.ask_route(task, feats, True)
        judge.join_shadow(15.0)
        return judge, ref

    def records(self):
        path = os.path.join(self.home, 'log', 'runs.jsonl')
        if not os.path.isfile(path):
            return []
        with open(path, encoding='utf-8') as fh:
            recs = [json.loads(ln) for ln in fh if ln.strip()]
        return [r for r in recs if r.get('role') == 'judge']

    def statuses(self):
        return [r['status'] for r in self.records()]


class JudgeFacadeTest(JudgeCase):
    def test_shadow_ask_is_async_and_logs_one_full_record(self):
        mod = self.mod()
        self.assertIs(importlib.import_module('mr.judge').Judge, mod.Judge)
        gate = threading.Event()
        rec = Recorder(gate=gate)
        judge = self.new_judge(self.cfg(), rec)
        task, feats = self.make()
        ref = judge.ask_route(task, feats, True)
        self.assertEqual(ref, 'j-' + TID + '-1')
        self.assertEqual(self.records(), [], 'in shadow ask_route returns before the call ends')
        gate.set()
        judge.join_shadow(15.0)
        recs = self.records()
        self.assertEqual(len(recs), 1, recs)
        r = recs[0]
        self.assertEqual([k for k in RECORD_KEYS if k not in r], [])
        got = tuple(r[k] for k in ('ref', 'use', 'qset', 'mode', 'backend', 'model_req', 'model_served', 'status',
                                   'consulted', 'applied', 'overridden_by'))
        self.assertEqual(got, (ref, 'route', 'route.v1', 'shadow', 'haiku', HAIKU, HAIKU, 'ok', True, [], {}))
        self.assertEqual(r['qset_sha'], qmod.qset_sha(qmod.load('route.v1')))
        self.assertEqual(r['prompt_sha'], prompt_mod.prompt_sha())
        self.assertEqual(r['pts_est'], 0.2)
        self.assertTrue(r['answers'])
        self.assertTrue(r['decisions'])
        self.assertEqual(len(rec.calls), 1)
        self.assertIn(HAIKU, rec.calls[0])
        self.assertEqual(judge.calls(), 1)
        ans = judge.route_answer()
        got = (ans.source, ans.status, ans.ref, ans.route, ans.type, ans.difficulty, ans.risk)
        self.assertEqual(got, ('judge', 'ok', ref, 'design', 'docs', 'critical', 'high'))

    def test_record_never_holds_state_prompt_or_env_values(self):
        rec = Recorder()
        self.ask(self.cfg(), rec, text='maybe fix the ' + CANARY + ' typo in README.md?')
        self.assertEqual(len(rec.calls), 1)
        self.assertIn(CANARY, ' '.join(rec.calls[0]), 'the planted canary must reach the prompt')
        recs = self.records()
        self.assertEqual(len(recs), 1)
        for key in ('state', 'prompt', 'env', 'redacted_keys'):
            self.assertNotIn(key, recs[0])
        text = json.dumps(recs)
        self.assertNotIn(CANARY, text)
        self.assertNotIn(ENV_CANARY, text)

    def test_second_identical_call_is_a_cache_hit(self):
        rec = Recorder()
        self.ask(self.cfg(), rec, task_id='t-0000c0a1')
        judge, ref = self.ask(self.cfg(), rec, task_id='t-0000c0a2')
        self.assertEqual(self.statuses(), ['ok', 'cache_hit'])
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(judge.calls(), 0)
        second = self.records()[1]
        self.assertEqual((second['ref'], second['pts_est']), (ref, 0))
        self.assertEqual(judge.route_answer().status, 'cache_hit')
        with open(os.path.join(self.home, 'state', 'judge-budget.json'), encoding='utf-8') as fh:
            self.assertEqual(json.load(fh)['calls_day'], 1, 'a cache hit is not charged')

    def test_budget_denied_after_the_task_cap(self):
        rec = Recorder()
        cfg = self.cfg(budget={'max_calls_per_task': 1})
        self.ask(cfg, rec, text='maybe fix typo in README.md?')
        judge, _ = self.ask(cfg, rec, text='maybe fix wording in README.md?')
        self.assertEqual(self.statuses(), ['ok', 'budget_denied'])
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(judge.calls(), 0)

    def test_breaker_opens_after_three_failures(self):
        rec = Recorder(rc=1, out='')
        for i in range(4):
            self.ask(self.cfg(), rec, text=f'maybe fix typo number {i} in README.md?', task_id=f't-0000b0a{i}')
        self.assertEqual(self.statuses(), ['cli_error'] * 3 + ['breaker_open'])
        self.assertEqual(len(rec.calls), 3)

    def test_slow_call_times_out_at_join_and_child_dies(self):
        pidfile = os.path.join(self.dir, 'judge-child.pid')
        slow = self.write_wrapper('slow-claude', ['#!/bin/sh', 'echo $$ > ' + pidfile, 'exec sleep 30'])
        judge = self.new_judge(self.cfg(claude=slow))
        task, feats = self.make()
        ref = judge.ask_route(task, feats, True)
        self.assertTrue(_wait(lambda: os.path.isfile(pidfile) and os.path.getsize(pidfile) > 0),
                        'the judge child never started')
        with open(pidfile, encoding='utf-8') as fh:
            pid = int(fh.read().strip())
        judge.join_shadow(0.3)
        self.assertTrue(_wait(lambda: not _alive(pid)), 'join_shadow left the judge child running')
        self.assertEqual([(r['ref'], r['status']) for r in self.records()], [(ref, 'timeout')])

    def test_missing_or_unrunnable_cli_is_cli_unavailable(self):
        fn = getattr(guard, 'judge_cli_available', None)
        self.assertTrue(callable(fn), 'guard.judge_cli_available is missing')
        missing = os.path.join(self.dir, 'missing', 'claude')
        plain = self.write_wrapper('plain-claude', ['#!/bin/sh', 'exit 0'], mode=0o644)
        self.assertTrue(fn(self.cfg()))
        self.assertFalse(fn(self.cfg(claude=missing)))
        self.assertFalse(fn(self.cfg(claude=plain)))
        rec = Recorder()
        self.ask(self.cfg(claude=missing), rec)
        self.ask(self.cfg(claude=plain), rec, text='maybe fix wording in README.md?', task_id='t-0000d7a2')
        self.assertEqual(self.statuses(), ['cli_unavailable', 'cli_unavailable'])
        self.assertEqual(rec.calls, [])

    def test_repo_allow_list(self):
        rec = Recorder()
        self.ask(self.cfg(repos=['elsewhere']), rec)
        self.assertEqual(self.statuses(), ['skipped_explicit'])
        self.assertEqual(rec.calls, [])
        self.ask(self.cfg(repos=[os.path.basename(self.repo)]), rec, task_id='t-0000d7a3')
        self.assertEqual(self.statuses(), ['skipped_explicit', 'ok'])
        self.assertEqual(len(rec.calls), 1)


class JudgeConfigTest(JudgeCase):
    def test_judge_sample_n_and_repos_defaults(self):
        for bad in (-1, 'three', 1.5):
            with self.assertRaises(config.ConfigError, msg=repr(bad)):
                config.from_dict(config._deep_merge(defaults(), {'judge': {'sample_n': bad}}))
        for good in (0, 1, 3):
            cfg = config.from_dict(config._deep_merge(defaults(), {'judge': {'sample_n': good}}))
            self.assertEqual(cfg.judge['sample_n'], good)
        raw = defaults()
        self.assertEqual(raw['judge']['repos'], 'all')
        self.assertEqual(raw['judge']['sample_n'], 3)
        self.assertEqual(config.from_dict(defaults()).judge['sample_n'], 3)


class AdversarialDecideTest(JudgeCase):
    def test_adversarial_judge_never_changes_decide(self):
        judge, _ = self.ask(self.cfg(), Recorder())
        ans = judge.route_answer()
        got = (ans.route, ans.type, ans.difficulty, ans.risk, len(ans.risk_tags))
        self.assertEqual(got, ('design', 'docs', 'critical', 'high', 7))
        self.assertEqual(self.records()[0]['applied'], [])
        cfg = self.cfg()
        rnd = random.Random(7)
        checked = 0
        for i in range(200):
            text = rnd.choice(VERBS) + ' ' + rnd.choice(OBJECTS) + rnd.choice(TAILS)
            params = Params(type=rnd.choice(TYPES), difficulty=rnd.choice(DIFFS), route=rnd.choice(ROUTES))
            task = make_task(text, self.repo, '0' * 40, params)
            feats = prepass.extract(task, None)
            case = (i, text, params)
            try:
                plain = router.decide(task, feats, cfg)
            except Exception as exc:
                with self.assertRaises(type(exc), msg=str(case)):
                    router.decide(task, feats, cfg, judge=ans)
                continue
            shadowed = router.decide(task, feats, cfg, judge=ans)
            self.assertEqual(shadowed, plain, case)
            rec = router.route_record(task, feats, shadowed, judge=ans)
            applied = dict(rec['applied'])
            self.assertEqual(applied.pop('ladder'), list(plain.ladder), case)
            self.assertEqual(applied, rec['rules'], case)
            self.assertEqual((rec['judge']['route'], rec['judge']['acted_fields']), ('design', []), case)
            checked += 1
        self.assertGreaterEqual(checked, 100)


if __name__ == '__main__':
    unittest.main()
