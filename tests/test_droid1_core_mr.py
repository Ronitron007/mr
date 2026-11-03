'''DROID1: droid config, settings file, argv, child env, failure classes, availability and run_droid.

Fake droid only (a script written to a temp dir); no network, no real droid, no real key.
'''
import importlib
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

from mr import config, runners
from mr.defaults import defaults

KEY = 'fake' + '-anthropic-' + 'value-' + '7f3a9c2e51'  # gitleaks:allow (fake test value)
FACTORY = 'fake' + '-factory-' + 'value-' + '0b1c9d'  # gitleaks:allow (fake test value)
NL = chr(10)
DEFAULT_RESULT = 'done' + NL + 'MR_RESULT ' + json.dumps(
    {'item': None, 'status': 'done', 'requirements': [], 'deviations': [], 'pushback': None, 'confidence': 0.8})


def api_error(code, kind, message):
    return f'{code} ' + json.dumps({'type': 'error', 'error': {'type': kind, 'message': message}})


CREDITS_MSG = api_error(400, 'invalid_request_error', 'Your credit balance is too low to access the Anthropic API.')
AUTH_MSG = api_error(401, 'authentication_error', 'invalid x-api-key')
RATE_MSG = api_error(429, 'rate_limit_error', 'slow down')
TWINS = {
    'S': {'model': 'claude-sonnet-5-5', 'name': 'mr-sonnet-high', 'thinking_tokens': 16000, 'max_output': 32000},
    'S+': {'model': 'claude-sonnet-5-5', 'name': 'mr-sonnet-xhigh', 'thinking_tokens': 32000, 'max_output': 64000},
}
FORBIDDEN_FLAGS = ('--skip-permissions-unsafe', '--worktree', '-w', '--mission', '-r', 'high')

FAKE = """import json
import os
import sys

steps_path = os.environ['DROID1_FAKE']
argv = sys.argv[1:]
calls_path = steps_path + '.calls.jsonl'
prior = 0
if os.path.isfile(calls_path):
    with open(calls_path, encoding='utf-8') as fh:
        prior = sum(1 for ln in fh if ln.strip() and json.loads(ln)['argv'][:1] == ['exec'])
keys = [k for k in ('ANTHROPIC_API_KEY', 'FACTORY_API_KEY') if os.environ.get(k)]
with open(calls_path, 'a', encoding='utf-8') as fh:
    fh.write(json.dumps({'argv': argv, 'cwd': os.getcwd(), 'keys': keys}) + chr(10))
if argv[:1] == ['--version']:
    print('droid 1.1.0')
    sys.exit(0)
with open(steps_path, encoding='utf-8') as fh:
    steps = json.load(fh)
step = steps[min(prior, len(steps) - 1)] if steps else {}
tree = argv[argv.index('--cwd') + 1] if '--cwd' in argv else os.getcwd()
for rel, text in (step.get('files') or {}).items():
    dest = os.path.join(tree, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, 'w', encoding='utf-8') as fh:
        fh.write(text)
result = step.get('result', @DEFAULT@)
if step.get('echo_key'):
    key = os.environ.get('ANTHROPIC_API_KEY', '')
    sys.stderr.write('debug: key ' + key + chr(10))
    result = 'saw ' + key + chr(10) + result
is_error = bool(step.get('is_error', False))
usage = {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 0,
         'cache_creation_input_tokens': 0, 'factory_credits': 0, 'thinking_tokens': 5, 'ttft_ms': 300}
print(json.dumps({'type': 'result', 'subtype': 'failure' if is_error else 'success', 'is_error': is_error,
                  'duration_ms': 1200, 'num_turns': step.get('num_turns', 3), 'result': result,
                  'session_id': 'fake-droid-1', 'usage': usage}))
sys.exit(int(step.get('rc', 0)))
"""


def write_steps(path, steps):
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(steps, fh)


def make_fake_droid(dirpath):
    '''Write the fake droid (a sh wrapper around a python script). Returns (binary, steps path).'''
    script = os.path.join(dirpath, 'fake_droid_impl.py')
    with open(script, 'w', encoding='utf-8') as fh:
        fh.write(FAKE.replace('@DEFAULT@', repr(DEFAULT_RESULT)))
    binary = os.path.join(dirpath, 'droid')
    with open(binary, 'w', encoding='utf-8') as fh:
        fh.write('#!/bin/sh' + NL + 'exec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(script)
                 + ' "$@"' + NL)
    os.chmod(binary, 0o755)
    steps = os.path.join(dirpath, 'droid-steps.json')
    write_steps(steps, [])
    return binary, steps


def exec_calls(steps_path):
    '''Recorded `droid exec` calls (a --version call is not one).'''
    path = steps_path + '.calls.jsonl'
    if not os.path.isfile(path):
        return []
    with open(path, encoding='utf-8') as fh:
        calls = [json.loads(ln) for ln in fh if ln.strip()]
    return [c for c in calls if c['argv'][:1] == ['exec']]


def _raw(home, binary, **droid):
    raw = defaults()
    raw['paths']['home'] = home
    raw['paths']['droid'] = binary
    block = {'enabled': True, 'settings': '', 'auto': 'medium', 'recheck_s': 3600,
             'provider_key_env': {'anthropic': 'ANTHROPIC_API_KEY'}, 'twins': json.loads(json.dumps(TWINS))}
    block.update(droid)
    raw['droid'] = block
    return raw


class DroidCase(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='mr-droid1-'))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, 'home')
        os.makedirs(self.home)
        self.bin, self.steps = make_fake_droid(self.dir)
        env = {'MR_HOME': self.home, 'ANTHROPIC_API_KEY': KEY, 'FACTORY_API_KEY': FACTORY,
               'DROID1_FAKE': self.steps}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def cfg(self, binary=None, **droid):
        return config.from_dict(_raw(self.home, binary or self.bin, **droid))

    def droid(self):
        try:
            return importlib.import_module('mr.droid')
        except ImportError as exc:
            self.fail(f'mr.droid is missing: {exc}')

    def block(self, cfg):
        block = getattr(cfg, 'droid', None)
        self.assertIsInstance(block, dict, 'Config has no droid block')
        return block


class TestDroidConfig(DroidCase):
    def test_defaults_ship_droid_off(self):
        raw = defaults()
        raw['paths']['home'] = self.home
        cfg = config.from_dict(raw)
        block = self.block(cfg)
        self.assertIs(block['enabled'], False)
        self.assertEqual((block['auto'], block['recheck_s']), ('medium', 10800))
        self.assertEqual(block['provider_key_env'], {'anthropic': 'ANTHROPIC_API_KEY'})
        self.assertEqual(block['settings'], os.path.join(self.home, 'droid', 'settings.json'))
        self.assertEqual(sorted(block['twins']), ['S', 'S+'])
        for code, twin in TWINS.items():
            for key, val in twin.items():
                self.assertEqual(block['twins'][code][key], val, (code, key))
        self.assertEqual(getattr(cfg.paths, 'droid', None), 'droid')
        self.assertEqual(cfg.timeouts.rung_s.get('droid'), 1800)
        ok, why = self.droid().available(cfg, now=1000.0)
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_enabled_twins_load_and_absent_block_is_off(self):
        cfg = self.cfg()
        block = self.block(cfg)
        self.assertIs(block['enabled'], True)
        self.assertEqual(block['twins']['S']['provider'], 'anthropic')
        self.assertEqual(block['twins']['S+']['provider'], 'anthropic')
        raw = _raw(self.home, self.bin)
        raw.pop('droid')
        off = config.from_dict(raw)
        self.assertIsNone(getattr(off, 'droid', 'missing'))
        self.assertEqual((off.rungs, off.ladders), (cfg.rungs, cfg.ladders))
        self.assertFalse(self.droid().available(off, now=1000.0)[0])

    def test_twin_rules_reject_bad_configs(self):
        s_twin, sp_twin = TWINS['S'], TWINS['S+']
        cases = [
            ('twin on an Opus rung', {'twins': {'O': dict(s_twin)}}, None),
            ('Opus twin model', {'twins': {'S': dict(s_twin, model='claude-opus-5-5')}}, None),
            ('Haiku twin model', {'twins': {'S': dict(s_twin, model='claude-haiku-5-5')}}, None),
            ('twin on a cursor rung', {'twins': {'G-x': dict(s_twin)}}, None),
            ('twin for the planner', {'twins': {'planner': dict(s_twin)}}, None),
            ('twin for review', {'twins': {'review': dict(s_twin)}}, None),
            ('twin on the advisor rung', {'twins': {'S+A': dict(sp_twin)}}, None),
            ('medium effort rung', {}, ('S', 'medium')),
            ('max effort rung', {}, ('S+', 'max')),
            ('provider other than anthropic', {'twins': {'S': dict(s_twin, provider='xai')}}, None),
            ('auto high', {'auto': 'high'}, None),
            ('duplicate twin names', {'twins': {'S': dict(s_twin), 'S+': dict(sp_twin, name=s_twin['name'])}}, None),
        ]
        for label, over, effort in cases:
            raw = _raw(self.home, self.bin, **over)
            if effort:
                raw['rungs'][effort[0]]['effort'] = effort[1]
            with self.assertRaises(config.ConfigError, msg=label):
                config.from_dict(raw)


class TestSettings(DroidCase):
    def test_settings_doc_uses_env_reference_only(self):
        d = self.droid()
        doc = d.settings_doc(self.cfg())
        models = doc['customModels']
        self.assertEqual([m['displayName'] for m in models], ['mr-sonnet-high', 'mr-sonnet-xhigh'])
        self.assertEqual([m['thinkingMaxTokens'] for m in models], [16000, 32000])
        self.assertEqual([m['maxOutputTokens'] for m in models], [32000, 64000])
        for m in models:
            self.assertEqual(m['apiKey'], '${ANTHROPIC_API_KEY}')
            self.assertEqual((m['model'], m['provider'], m['baseUrl']),
                             ('claude-sonnet-5-5', 'anthropic', 'https://api.anthropic.com'))
            self.assertIs(m['enableThinking'], True)
        self.assertNotIn(KEY, json.dumps(doc))
        self.assertEqual(d.model_id(TWINS['S']), 'custom:mr-sonnet-high-0')
        self.assertEqual(d.model_id(TWINS['S+']), 'custom:mr-sonnet-xhigh-0')

    def test_ensure_settings_0600_and_refuses_literal_key(self):
        d, cfg = self.droid(), self.cfg()
        path = self.block(cfg)['settings']
        self.assertEqual(path, os.path.join(self.home, 'droid', 'settings.json'))
        self.assertEqual(d.ensure_settings(cfg), 'written')
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        self.assertEqual(json.loads(text), d.settings_doc(cfg))
        self.assertNotIn(KEY, text)
        self.assertEqual(d.ensure_settings(cfg), 'ok')
        doc = d.settings_doc(cfg)
        doc['customModels'][0]['apiKey'] = 'literal' + '-key-' + '123456'
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh)
        with self.assertRaises(config.ConfigError):
            d.ensure_settings(cfg)
        with open(path, encoding='utf-8') as fh:
            self.assertIn('literal-key-123456', fh.read())


class TestArgvAndEnv(DroidCase):
    def test_droid_argv_exact_for_s_and_s_plus(self):
        build = getattr(runners, 'droid_argv', None)
        self.assertIsNotNone(build, 'runners.droid_argv is missing')
        cfg = self.cfg()
        block = self.block(cfg)
        prompt, tree = os.path.join(self.dir, 'p.txt'), os.path.join(self.dir, 'tree')
        for code, mid in (('S', 'custom:mr-sonnet-high-0'), ('S+', 'custom:mr-sonnet-xhigh-0')):
            argv = build(cfg, block['twins'][code], prompt, tree)
            self.assertEqual(argv, [self.bin, 'exec', '-o', 'json', '--settings', block['settings'], '-m', mid,
                                    '--auto', 'medium', '--cwd', tree, '--disable-builtin-skills', '-f', prompt],
                             code)
            for flag in FORBIDDEN_FLAGS:
                self.assertNotIn(flag, argv, (code, flag))

    def test_child_envs_route_the_key(self):
        build = getattr(runners, 'droid_child_env', None)
        self.assertIsNotNone(build, 'runners.droid_child_env is missing')
        env = build(self.cfg())
        self.assertEqual(env.get('ANTHROPIC_API_KEY'), KEY)
        self.assertNotIn('FACTORY_API_KEY', env)
        self.assertEqual(env.get('GIT_CONFIG_COUNT'), '3')
        for label, child in (('claude', runners.child_env(True)), ('cursor', runners.child_env(False)),
                             ('advisor', runners.child_env(True, advisor=True))):
            self.assertNotIn('ANTHROPIC_API_KEY', child, label)
            self.assertNotIn('FACTORY_API_KEY', child, label)


class TestFailuresAndAvailability(DroidCase):
    def test_classify_failure_classes(self):
        d = self.droid()
        cases = [
            (CREDITS_MSG, '', 'credits'), ('402 Payment Required', '', 'credits'),
            ('billing problem on the account', '', 'credits'), ('insufficient balance', '', 'credits'),
            (AUTH_MSG, '', 'auth'), (api_error(403, 'permission_error', 'forbidden'), '', 'auth'),
            ('', 'error: authentication_error', 'auth'),
            (RATE_MSG, '', 'transient'), (api_error(529, 'overloaded_error', 'Overloaded'), '', 'transient'),
            ('Overloaded, try later', '', 'transient'),
            ('tests failed: see output', '', 'other'), ('', '', 'other'),
        ]
        for text, err, want in cases:
            self.assertEqual(d.classify_failure(text, err), want, (text, err))

    def test_available_follows_state_key_and_binary(self):
        d, cfg = self.droid(), self.cfg()
        self.assertTrue(d.available(cfg, now=1000.0)[0], 'enabled, key set, binary present, no state')
        path = os.path.join(self.home, 'state', 'droid.json')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'status': 'credits_exhausted', 'since': 500.0, 'recheck_after': 2000.0,
                       'detail': 'credit balance is too low'}, fh)
        ok, why = d.available(cfg, now=1000.0)
        self.assertFalse(ok)
        self.assertTrue(why)
        self.assertTrue(d.available(cfg, now=3000.0)[0], 'recheck time passed')
        os.remove(path)
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': ''}):
            self.assertFalse(d.available(cfg, now=1000.0)[0], 'empty key')
        self.assertFalse(d.available(self.cfg(binary=os.path.join(self.dir, 'no-such-droid')), now=1000.0)[0])
        self.assertFalse(d.available(self.cfg(enabled=False), now=1000.0)[0])


class TestRunDroid(DroidCase):
    def test_run_droid_parses_envelope_and_never_leaks_key(self):
        run = getattr(runners, 'run_droid', None)
        self.assertIsNotNone(run, 'runners.run_droid is missing')
        cfg = self.cfg()
        tree = os.path.join(self.dir, 'tree')
        os.makedirs(tree)
        write_steps(self.steps, [{'echo_key': True}])
        rr = run('S', self.block(cfg)['twins']['S'], 'do the task', tree, 30, cfg=cfg, task='t-droid1', attempt=1)
        self.assertEqual(getattr(rr, 'backend', None), 'droid')
        self.assertIsNone(getattr(rr, 'droid_failure', 'missing'))
        self.assertFalse(rr.is_error)
        self.assertEqual((rr.model_used, rr.session_id, rr.duration_ms), ('claude-sonnet-5-5', 'fake-droid-1', 1200))
        self.assertEqual(rr.usage.get('factory_credits'), 0)
        self.assertEqual((rr.mr_result or {}).get('status'), 'done')
        calls = exec_calls(self.steps)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['keys'], ['ANTHROPIC_API_KEY'])
        argv = calls[0]['argv']
        self.assertEqual(argv[argv.index('--cwd') + 1], tree)
        self.assertTrue(argv[argv.index('-f') + 1].startswith(os.path.join(self.home, 'raw') + os.sep))
        self.assertTrue(os.path.isfile(self.block(cfg)['settings']))
        self.assertNotIn(KEY, rr.result_text)
        self.assertNotIn(KEY, rr.stderr_tail)
        seen = 0
        for root, _dirs, files in os.walk(self.home):
            for name in files:
                seen += 1
                with open(os.path.join(root, name), encoding='utf-8', errors='replace') as fh:
                    self.assertNotIn(KEY, fh.read(), os.path.join(root, name))
        self.assertGreaterEqual(seen, 3)  # settings, prompt and raw output files


if __name__ == '__main__':
    unittest.main()
