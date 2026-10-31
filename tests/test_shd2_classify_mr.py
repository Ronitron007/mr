'''SHD2 locked tests: mr.classifier.classify with a fake exec_fn, the answer cache, record() and to_shadow().'''
import dataclasses
import importlib
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from mr.config import from_dict
from mr.defaults import defaults
from mr.prepass import extract_from
from mr.shadow import ShadowAnswer
from mr.task import Params, Task
from tests import log_schema_v2

NOW = 1_800_000_000.0  # injected clock (epoch seconds); tests never read the real clock
DAY = 86400.0
REF = 'c-t-0000abcd-1'
# Split so the public-tree gate never sees a key-like literal.
FAKE_KEY = 'gh' + 'p_' + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8'
STAMP = {'ts': '2026-11-01T10:00:00+05:30', 'v': 2, 'mr_version': '0.1.0'}
VALID = {'task_type': 'bugfix', 'difficulty': 'easy', 'risk': 'low', 'risk_tags': ['none'],
         'load_bearing': False, 'high_judgment': False, 'scope_files_est': 2, 'ambiguity': 'low',
         'novelty': 'mixed', 'context': 'small', 'model': 'G-m', 'effort': 'medium',
         'needs_worktree': False, 'needs_review': False, 'needs_clarification': False,
         'confidence': 0.8, 'reason': 'small typo fix'}
TIMEOUT = (-15, '', '', 30.0, True)


def _clf(case):
    try:
        return importlib.import_module('mr.classifier')
    except ImportError as exc:
        case.fail(f'mr.classifier is missing: {exc}')


def _line(**kw):
    return json.dumps({**VALID, **kw}, separators=(',', ':'))


def _envelope(text):
    return json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text})


class FakeExec:
    '''Stands in for runners.exec_child: records each call and returns canned replies in order.'''

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, argv, cwd, env, timeout_s, raw_base):
        self.calls.append({'argv': list(argv), 'cwd': cwd, 'env': dict(env), 'timeout_s': timeout_s,
                           'raw_base': raw_base})
        if not self.replies:
            raise AssertionError('unexpected cursor-agent call')
        reply = self.replies.pop(0)
        if isinstance(reply, tuple):
            return reply
        return 0, _envelope(reply), '', 0.25, False


class ClassifierCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        patcher = mock.patch.dict(os.environ, {'MR_HOME': self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.agent = os.path.join(self.tmp, 'cursor-agent')
        with open(self.agent, 'w', encoding='utf-8') as fh:
            fh.write('')
        os.chmod(self.agent, 0o755)
        base = from_dict(defaults())
        self.cfg = dataclasses.replace(
            base, paths=dataclasses.replace(base.paths, cursor_agent=self.agent, home=self.home))
        self.repo = os.path.join(self.tmp, 'sample-repo')
        os.makedirs(self.repo)
        self.task = self.make_task('fix the typo in the readme heading')

    def make_task(self, text, files=('docs/guide.md',)):
        return Task(id='t-0000abcd', text=text, repo=self.repo, base='a' * 40, params=Params(),
                    files_named=files, source='cli')

    def classify(self, fake, task=None, cfg=None, now=NOW):
        c = _clf(self)
        task = task or self.task
        return c.classify(task, extract_from(task.text, task.files_named), cfg or self.cfg, ref=REF,
                          exec_fn=fake, now=now)

    def with_paths(self, **kw):
        return dataclasses.replace(self.cfg, paths=dataclasses.replace(self.cfg.paths, **kw))


class ArgvEnvTest(ClassifierCase):
    def test_argv_golden_and_empty_workdir(self):
        fake = FakeExec(_line())
        self.classify(fake)
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        workdir = os.path.join(self.home, 'classifier-workdir')
        self.assertEqual(call['argv'][:-1], [self.agent, '-p', '--output-format', 'json', '--mode', 'ask',
                                             '--model', 'grok-4.7-low', '--trust', '--workspace', workdir])
        self.assertNotIn('--force', call['argv'])
        self.assertTrue(os.path.isdir(workdir))
        self.assertEqual(os.listdir(workdir), [])
        self.assertNotEqual(call['cwd'], self.repo)
        self.assertEqual(call['timeout_s'], 30)
        prefix = os.path.join(self.home, 'raw', 't-0000abcd', 'classifier-')
        self.assertTrue(call['raw_base'].startswith(prefix), call['raw_base'])

    def test_prompt_names_repo_files_and_allowed_codes(self):
        fake = FakeExec(_line())
        self.classify(fake)
        prompt = fake.calls[0]['argv'][-1]
        self.assertIn('sample-repo', prompt)
        self.assertNotIn(self.tmp, prompt)
        self.assertIn('docs/guide.md', prompt)
        self.assertIn('fix the typo in the readme heading', prompt)
        for code in ('G-l', 'G-x', 'C25', 'O+', 'S+'):
            self.assertIn(code, prompt)
        for code in ('S+A', 'K-h', 'K-x'):
            self.assertNotIn(code, prompt)

    def test_env_drops_cursor_token_and_secrets(self):
        fake = FakeExec(_line())
        planted = {'CURSOR_AUTH_TOKEN': 'fake-session-value', 'CLOUDFLARE_API_TOKEN': 'fake-cf-value',
                   'OPENROUTER_API_KEY': 'fake-or-value'}
        with mock.patch.dict(os.environ, planted):
            self.classify(fake)
        env = fake.calls[0]['env']
        for key in planted:
            self.assertNotIn(key, env)
        self.assertEqual(env.get('GIT_CONFIG_COUNT'), '3')

    def test_task_text_redacted_and_capped(self):
        fake = FakeExec(_line(), _line())
        res = self.classify(fake, task=self.make_task(f'rotate the key {FAKE_KEY} in config'))
        prompt = fake.calls[0]['argv'][-1]
        self.assertNotIn(FAKE_KEY, prompt)
        self.assertIn('<redacted:', prompt)
        self.assertGreaterEqual(sum(res.redactions.values()), 1)
        self.classify(fake, task=self.make_task('fix ' + 'z' * 5000))
        long_prompt = fake.calls[1]['argv'][-1]
        self.assertIn('z' * 3990, long_prompt)
        self.assertNotIn('z' * 4001, long_prompt)


class StatusTest(ClassifierCase):
    def test_valid_and_prose_replies_ok(self):
        c = _clf(self)
        res = self.classify(FakeExec(_line()))
        self.assertEqual(res.status, 'ok')
        self.assertEqual(res.calls, 1)
        self.assertEqual(res.out.task_type, 'bugfix')
        self.assertEqual(res.ref, REF)
        self.assertEqual(res.prompt_sha, c.prompt_sha())
        self.assertIsInstance(res.latency_ms, int)
        prose = FakeExec('Draft {} then final: ' + _line(task_type='test') + ' done.')
        res2 = self.classify(prose, task=self.make_task('add a unit test for the parser'))
        self.assertEqual(res2.status, 'ok')
        self.assertEqual(res2.calls, 1)
        self.assertEqual(res2.out.task_type, 'test')

    def test_invalid_then_valid_repairs_once(self):
        fake = FakeExec('I cannot answer in JSON.', _line())
        res = self.classify(fake)
        self.assertEqual(res.status, 'ok')
        self.assertEqual(res.calls, 2)
        self.assertEqual(len(fake.calls), 2)
        first, second = fake.calls[0]['argv'], fake.calls[1]['argv']
        self.assertEqual(second[:-1], first[:-1])
        self.assertTrue(second[-1].startswith(first[-1]))
        self.assertGreater(len(second[-1]), len(first[-1]))

    def test_invalid_twice_gives_none_and_is_not_cached(self):
        c = _clf(self)
        res = self.classify(FakeExec('not json', _line(difficulty='extreme')))
        self.assertIsNone(res.out)
        self.assertEqual(res.status, 'invalid')
        self.assertEqual(res.calls, 2)
        rec = c.record(res, self.task, 'shadow', True)
        self.assertEqual(rec['reason'], 'classifier_invalid')
        self.assertIsNone(rec['answers'])
        self.assertEqual(rec['status'], 'invalid')
        again = FakeExec(_line())
        self.assertEqual(self.classify(again).status, 'ok')
        self.assertEqual(len(again.calls), 1)

    def test_timeout_is_not_retried(self):
        fake = FakeExec(TIMEOUT)
        res = self.classify(fake)
        self.assertEqual(res.status, 'timeout')
        self.assertEqual(res.calls, 1)
        self.assertIsNone(res.out)
        self.assertEqual(len(fake.calls), 1)

    def test_nonzero_rc_or_no_envelope_is_cli_error(self):
        fake = FakeExec((1, _envelope(_line()), 'boom', 0.1, False))
        res = self.classify(fake)
        self.assertEqual(res.status, 'cli_error')
        self.assertEqual(res.calls, 1)
        self.assertIsNone(res.out)
        self.assertEqual(len(fake.calls), 1)
        bare = FakeExec((0, 'plain text, no envelope', '', 0.1, False))
        res2 = self.classify(bare, task=self.make_task('another small task'))
        self.assertEqual(res2.status, 'cli_error')
        self.assertEqual(len(bare.calls), 1)

    def test_refused_model_and_missing_cli_make_no_call(self):
        fake = FakeExec()
        claude = dataclasses.replace(self.cfg, classifier={**self.cfg.classifier, 'model': 'claude-sonnet-5-5'})
        res = self.classify(fake, cfg=claude)
        self.assertEqual(res.status, 'refused_model')
        self.assertEqual(res.calls, 0)
        self.assertIsNone(res.out)
        noexec = os.path.join(self.tmp, 'agent-noexec')
        with open(noexec, 'w', encoding='utf-8') as fh:
            fh.write('')
        os.chmod(noexec, 0o644)
        for path in (os.path.join(self.tmp, 'no-such-agent'), 'mr-no-such-cursor-agent', noexec):
            with self.subTest(path=os.path.basename(path)):
                got = self.classify(fake, cfg=self.with_paths(cursor_agent=path))
                self.assertEqual(got.status, 'cli_unavailable')
                self.assertEqual(got.calls, 0)
        self.assertEqual(fake.calls, [])

    def test_redaction_abort_makes_no_call(self):
        fake = FakeExec()
        text = 'call ' + ' '.join(f'90000000{i:02d}' for i in range(26))
        res = self.classify(fake, task=self.make_task(text))
        self.assertEqual(res.status, 'redaction_abort')
        self.assertEqual(res.calls, 0)
        self.assertIsNone(res.out)
        self.assertGreater(sum(res.redactions.values()), 25)
        self.assertEqual(fake.calls, [])


class CacheTest(ClassifierCase):
    def test_cache_hit_skips_call(self):
        fake = FakeExec(_line())
        first = self.classify(fake, now=NOW)
        second = self.classify(fake, now=NOW + 60)
        self.assertEqual(first.status, 'ok')
        self.assertEqual(second.status, 'cache_hit')
        self.assertEqual(second.calls, 0)
        self.assertEqual(second.out, first.out)
        self.assertEqual(len(fake.calls), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.home, 'state', 'classifier-cache.sqlite')))

    def test_expired_cache_row_calls_again(self):
        fake = FakeExec(_line(), _line(task_type='docs'))
        self.assertEqual(self.classify(fake, now=NOW).status, 'ok')
        self.assertEqual(self.classify(fake, now=NOW + 6 * DAY).status, 'cache_hit')
        late = self.classify(fake, now=NOW + 8 * DAY)
        self.assertEqual(late.status, 'ok')
        self.assertEqual(late.calls, 1)
        self.assertEqual(late.out.task_type, 'docs')
        self.assertEqual(len(fake.calls), 2)


class RecordShadowTest(ClassifierCase):
    def ok(self, name, **kw):
        res = self.classify(FakeExec(_line(**kw)), task=self.make_task(f'variant task {name}'))
        self.assertEqual(res.status, 'ok')
        return res

    def test_record_passes_schema_without_text(self):
        c = _clf(self)
        res = self.classify(FakeExec(_line()))
        rec = c.record(res, self.task, 'shadow', False)
        self.assertEqual(log_schema_v2.check({**rec, **STAMP}), [])
        want = {'role': 'classifier', 'task': 't-0000abcd', 'ref': REF, 'mode': 'shadow',
                'model': 'grok-4.7-low', 'status': 'ok', 'consulted': False, 'calls': 1,
                'prompt_sha': c.prompt_sha(), 'reason': None}
        self.assertEqual({k: rec.get(k) for k in want}, want)
        self.assertEqual(rec['answers']['task_type'], 'bugfix')
        self.assertIsInstance(rec['redactions'], dict)
        self.assertIn('latency_ms', rec)
        self.assertNotIn('prompt', rec)
        self.assertNotIn(self.task.text, json.dumps(rec))

    def test_to_shadow_route_and_fields(self):
        c = _clf(self)
        cases = [({'load_bearing': True, 'high_judgment': True}, 'design'), ({'high_judgment': True}, 'judgment'),
                 ({}, 'cascade')]
        for i, (kw, route) in enumerate(cases):
            with self.subTest(route=route):
                self.assertEqual(c.to_shadow(self.ok(str(i), **kw), True, cfg=self.cfg).route, route)
        res = self.ok('fields', risk_tags=['deps', 'none'])
        ans = c.to_shadow(res, False, cfg=self.cfg)
        self.assertIsInstance(ans, ShadowAnswer)
        self.assertEqual((ans.source, ans.mode, ans.status, ans.ref, ans.consulted),
                         ('classifier', 'shadow', 'ok', REF, False))
        self.assertEqual((ans.type, ans.difficulty, ans.risk), ('bugfix', 'easy', 'low'))
        self.assertEqual(ans.risk_tags, ('deps',))
        self.assertEqual(ans.confidence, 0.8)
        self.assertEqual(ans.first_rung, 'G-m')
        self.assertEqual(ans.unsure_fields, ())
        self.assertEqual(ans.latency_ms, res.latency_ms)
        for model in ('S+A', 'K-h', 'Z9', None):
            with self.subTest(model=model):
                got = c.to_shadow(self.ok(f'model {model}', model=model), False, cfg=self.cfg)
                self.assertIsNone(got.first_rung)

    def test_to_shadow_low_confidence_and_no_answer(self):
        c = _clf(self)
        low = c.to_shadow(self.ok('low', confidence=0.4), True, cfg=self.cfg)
        self.assertEqual(set(low.unsure_fields), set(VALID))
        self.assertEqual(low.route, 'cascade')
        res = self.classify(FakeExec(TIMEOUT), task=self.make_task('slow task'))
        ans = c.to_shadow(res, True, cfg=self.cfg)
        self.assertEqual((ans.source, ans.status, ans.consulted), ('classifier', 'timeout', True))
        for field in ('route', 'type', 'difficulty', 'risk', 'confidence', 'first_rung'):
            self.assertIsNone(getattr(ans, field), field)


if __name__ == '__main__':
    unittest.main()
