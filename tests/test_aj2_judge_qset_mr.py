'''AJ2 locked tests: judge.qset (default route.v2, validated), the Judge facade on route.v2 (expanded qset_sha,
files and tags readings in the judge record) and mr run / mr route passing the AJ1 facts to the judge.
Fake claude through exec_fn only; no network.'''
import contextlib
import importlib
import inspect
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from mr import cli, config, prepass
from mr.defaults import defaults
from mr.facts import Candidate, Facts, RiskItem
from mr.log import Log
from mr.task import Params, make_task
from tests import gitfix
from tests.test_run_once import CHECK_DEFAULT, RunCase

HAIKU = 'claude-haiku-5-5'
NL = chr(10)
TID = 't-0000a2c1'
TEXT = 'maybe fix the login token check in src/app.py?'
UNSURE = 'maybe fix typo in README.md or something?'
README = {'files': {'README.md': 'fixed typo'}}
FACTS = Facts(path_tokens=('src/app.py',), paths_exist={'src/app.py': True},
              candidates=(Candidate('src/app.py', True, ('named', 'exists')),
                          Candidate('src/util.py', True, ('symbol:check_token',))),
              risk_evidence=(RiskItem('auth', 'text', 'login token', None),), sensitive_paths=(),
              type_features={'lead_verb': None, 'test_only_scope': False, 'keyword_hits': {}}, repo_files=2)


def envelope(result):
    return json.dumps({
        'type': 'result', 'subtype': 'success', 'is_error': False, 'result': result, 'duration_ms': 900,
        'duration_api_ms': 700, 'total_cost_usd': 0.0012, 'usage': {'input_tokens': 900, 'output_tokens': 120},
        'modelUsage': {HAIKU: {'inputTokens': 900, 'outputTokens': 120}}})


def answers_for(questions, qmap):
    key = {qid: k for k, qid in qmap.items()}
    out = {}
    for qid, q in questions.items():
        if qid == 'files.change':
            ans = {'answer': ['src/app.py'], 'confidence': 0.9}
        elif qid == 'files.new':
            ans = {'answer': [], 'confidence': 0.8}
        elif qid.startswith('tag.'):
            ans = {'answer': True, 'confidence': 0.9, 'evidence': 'login token'}
        elif q.get('type') == 'choice':
            ans = {'answer': list(q['criteria'])[0], 'confidence': 0.9}
        elif q.get('type') == 'score':
            ans = {'answer': 0, 'confidence': 0.9}
        else:
            ans = {'answer': True, 'confidence': 0.9}
        out[key[qid]] = ans
    return json.dumps({'answers': out})


class Recorder:
    '''Stands in for runners.exec_child: (argv, cwd, env, timeout_s, raw_base) -> (rc, out, err, wall_s, timed_out).'''

    def __init__(self, out):
        self.calls = []
        self.reply = (0, out, '', 0.05, False)

    def __call__(self, argv, cwd, env, timeout_s, raw_base):
        self.calls.append(list(argv))
        return self.reply


class JudgeV2Case(unittest.TestCase):
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
        patcher = mock.patch.dict(os.environ, {'MR_HOME': self.home, 'MR_FAKE_CHECK': check,
                                               'MR_TEST_TIME_SCALE': '1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def cfg(self, **judge):
        over = {'paths': {'home': self.home, 'check': gitfix.FAKE_CHECK, 'claude': self.wrapper}}
        if judge:
            over['judge'] = judge
        return config.from_dict(config._deep_merge(defaults(), over))

    def records(self):
        path = os.path.join(self.home, 'log', 'runs.jsonl')
        if not os.path.isfile(path):
            return []
        with open(path, encoding='utf-8') as fh:
            recs = [json.loads(ln) for ln in fh if ln.strip()]
        return [r for r in recs if r.get('role') == 'judge']


class QsetConfigTest(JudgeV2Case):
    def test_qset_default_and_validation(self):
        self.assertEqual(defaults()['judge'].get('qset'), 'route.v2')
        self.assertEqual(config.from_dict(defaults()).judge.get('qset'), 'route.v2')
        cfg = config.from_dict(config._deep_merge(defaults(), {'judge': {'qset': 'route.v1'}}))
        self.assertEqual(cfg.judge['qset'], 'route.v1')
        for bad in ('route.v3', 'v2', 2):
            with self.assertRaises(config.ConfigError, msg=repr(bad)):
                config.from_dict(config._deep_merge(defaults(), {'judge': {'qset': bad}}))


class JudgeV2Test(JudgeV2Case):
    def test_v2_record_has_expanded_sha_files_and_tags(self):
        mod = importlib.import_module('mr.judge.judge')
        self.assertIn('facts', inspect.signature(mod.Judge.ask_route).parameters, 'ask_route takes facts=')
        qmod = importlib.import_module('mr.judge.questions')
        pmod = importlib.import_module('mr.judge.prompt')
        questions = qmod.expand_v2(qmod.load('route.v2'), FACTS)
        _, qmap = pmod.render_prompt_v2({'kind': 'mr.route.v2'}, questions)
        rec = Recorder(envelope(answers_for(questions, qmap)))
        judge = mod.Judge(self.cfg(), 'shadow', Log(self.home), task_id=TID, exec_fn=rec)
        task = make_task(TEXT, self.repo, '0' * 40, Params())
        ref = judge.ask_route(task, prepass.extract(task, None), True, facts=FACTS)
        judge.join_shadow(15.0)
        recs = self.records()
        self.assertEqual(len(recs), 1, recs)
        r = recs[0]
        self.assertEqual((r['ref'], r['qset'], r['status']), (ref, 'route.v2', 'ok'))
        self.assertEqual(r['qset_sha'], qmod.qset_sha(questions))
        self.assertEqual(r['decisions']['files']['change'], ['src/app.py'])
        auth = r['decisions']['tags']['auth']
        self.assertEqual((auth['changes_behavior'], auth['evidence'], auth['quote_found']), (True, 'login token', True))
        self.assertEqual(len(rec.calls), 1)
        self.assertIn('mr.route.v2', ' '.join(rec.calls[0]))
        self.assertIn('auth', judge.route_answer().risk_tags)

    def test_v2_builds_facts_when_caller_passes_none(self):
        mod = importlib.import_module('mr.judge.judge')
        facts_mod = importlib.import_module('mr.facts')
        rec = Recorder(envelope(json.dumps({'answers': {}})))
        with mock.patch.object(facts_mod, 'build_facts', return_value=FACTS) as built:
            judge = mod.Judge(self.cfg(), 'shadow', Log(self.home), task_id=TID, exec_fn=rec)
            task = make_task(TEXT, self.repo, '0' * 40, Params())
            judge.ask_route(task, prepass.extract(task, None), True)
            judge.join_shadow(15.0)
        self.assertEqual(built.call_count, 1)
        recs = self.records()
        self.assertEqual(len(recs), 1, recs)
        self.assertEqual(recs[0]['qset'], 'route.v2')
        qmod = importlib.import_module('mr.judge.questions')
        self.assertEqual(recs[0]['qset_sha'], qmod.qset_sha(qmod.expand_v2(qmod.load('route.v2'), FACTS)))


class FactsPassInTest(RunCase):
    def setUp(self):
        super().setUp()
        self.env['MR_HOME'] = self.home
        self.seen = []
        seen = self.seen

        def fake_ask(judge_self, task, features, consulted, facts=None):
            seen.append(facts)
            return 'j-' + task.id + '-1'

        self.fake_ask = fake_ask

    def test_route_passes_facts_to_the_judge(self):
        argv = ['--config', self.cfgpath, 'route', UNSURE, '--repo', self.repo, '--judge', 'shadow']
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('mr.judge.judge.Judge.ask_route', self.fake_ask), mock.patch.dict(os.environ, self.env), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.main(argv)
        self.assertEqual(len(self.seen), 1, out.getvalue() + err.getvalue())
        self.assertIsInstance(self.seen[0], Facts)
        self.assertIn('README.md', [c.path for c in self.seen[0].candidates])

    def test_run_passes_facts_to_the_judge(self):
        self.cursor(README)
        self.env['MR_HOME'] = self.home
        with mock.patch('mr.judge.judge.Judge.ask_route', self.fake_ask):
            code, out, err = self.mr('--judge', 'shadow', text=UNSURE)
        self.assertEqual(len(self.seen), 1, err)
        self.assertIsInstance(self.seen[0], Facts)
        self.assertIn('README.md', [c.path for c in self.seen[0].candidates])


if __name__ == '__main__':
    unittest.main()
