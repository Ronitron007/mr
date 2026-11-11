from __future__ import annotations

import json
import os
import shutil
import tempfile
import textwrap
import unittest
from unittest import mock

from mr import acceptance, config, patches, planner, scope
from mr.defaults import defaults
from mr.task import Params, Task
from tests import gitfix

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILE = 'tests/test_mod.py'
QUAL = 'tests.test_mod.TestCase'

_RED = textwrap.dedent('''
    import mul
    import unittest


    class TestMul(unittest.TestCase):
        def test_mul_basic(self):
            self.assertEqual(mul.mul(2, 3), 6)
''')

_CHECK_OK = {
    'hour_usage': 10, 'limit': 40, 'unit': 'points', 'exceeded': False,
    'five_hour_pct': 30.0, 'five_hour_resets_at': '2026-10-30T05:00:00Z',
    'seven_day_pct': 12.0, 'sample_age_s': 30, 'samples_in_hour': 3,
    'has_baseline': True, 'checked_at': '2026-10-30T00:00:00Z',
}


def _lines(n):
    return ''.join('v%d = %d\n' % (i, i) for i in range(n))


def _write(root, rel, text):
    path = os.path.join(root, rel)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as handle:
        return handle.read()


def _unit_doc(content, create, goal='Add mul.'):
    path = 'tests/test_mul_mr.py'
    return {
        'goal': goal,
        'scope': {
            'modify': ['README.md'],
            'create': list(create),
            'forbidden': ['package.json'],
        },
        'requirements': [{
            'id': 'R1',
            'text': 'mul SHALL multiply',
            'examples': [{'given': '2, 3', 'when': 'mul', 'then': '6'}],
        }],
        'acceptance': [{
            'id': 'A1',
            'covers': ['R1'],
            'file': path,
            'tests': ['test_mul_basic'],
        }],
        'est_loc': 10,
        'files_map': {'R1': ['mul.py']},
        'tests': [{'path': path, 'content': content}],
    }


def _header(name, tail):
    return '%s (%s) ...%s' % (name, QUAL, tail)


def _first_bullet(text):
    start = text.index('**Changelog**')
    for line in text[start:].splitlines():
        if line.startswith('- '):
            return line
    raise AssertionError('changelog bullet missing')


def _line_startswith(text, prefix):
    for line in text.splitlines():
        if line.startswith(prefix):
            return line
    raise AssertionError(prefix)


def _section(text, heading):
    start = text.index(heading)
    lines = text[start:].splitlines()
    kept = [lines[0]]
    for line in lines[1:]:
        if line.startswith('## ') and not line.startswith('###'):
            break
        kept.append(line)
    return '\n'.join(kept)


class TestAcceptanceGaps(unittest.TestCase):
    def test_split_status_after_warnings(self):
        def parse(text):
            return acceptance._parse_unittest(text, [FILE])

        inline = _header(
            'test_inline',
            ' /usr/lib/python3.13/warnings.py:110: ResourceWarning: unclosed file',
        ) + '\n' + 'ok\n'
        self.assertEqual(parse(inline).get((FILE, 'test_inline')), 'passed')

        split_ok = (
            _header('test_warn', ' ') + '\n'
            + 'ResourceWarning: unclosed file\n'
            + '  Enable tracemalloc to get the object allocation traceback\n'
            + 'ok\n'
        )
        self.assertEqual(parse(split_ok).get((FILE, 'test_warn')), 'passed')

        split_fail = _header('test_bad', ' ') + '\n' + 'ResourceWarning: unclosed file\n' + 'FAIL\n'
        self.assertEqual(parse(split_fail).get((FILE, 'test_bad')), 'failed')

        split_err = _header('test_boom', ' ') + '\n' + 'ResourceWarning: unclosed file\n' + 'ERROR\n'
        self.assertEqual(parse(split_err).get((FILE, 'test_boom')), 'failed')

        split_skip = _header('test_later', ' ') + '\n' + 'ResourceWarning: unclosed file\n' + 'skipped\n'
        self.assertEqual(parse(split_skip).get((FILE, 'test_later')), 'skipped')

        dangling = _header('test_open', ' ') + '\n' + 'ResourceWarning: unclosed file\n'
        self.assertIsNone(parse(dangling).get((FILE, 'test_open')))

        mixed = (
            _header('test_hang', ' ') + '\n'
            + 'ResourceWarning: unclosed file\n'
            + _header('test_next', ' ok') + '\n'
        )
        mixed_run = parse(mixed)
        self.assertIsNone(mixed_run.get((FILE, 'test_hang')))
        self.assertEqual(mixed_run.get((FILE, 'test_next')), 'passed')

        self.assertEqual(parse(_header('test_same', ' ok') + '\n').get((FILE, 'test_same')), 'passed')
        self.assertEqual(
            parse(_header('test_same_fail', ' FAIL') + '\n').get((FILE, 'test_same_fail')),
            'failed',
        )

    def test_unittest_child_env_ignores_warnings(self):
        captured = []

        def fake_popen(argv, **kwargs):
            captured.append((list(argv), kwargs.get('env'), os.environ.get('PYTHONWARNINGS')))
            proc = mock.Mock()
            proc.communicate.return_value = (b'', b'')
            proc.returncode = 0
            proc.pid = 123
            return proc

        with mock.patch.dict(os.environ, {'PYTHONWARNINGS': 'default'}):
            with mock.patch('mr.acceptance.subprocess.Popen', side_effect=fake_popen), \
                    mock.patch('mr.acceptance.purge_bytecode'):
                acceptance.run_named(tempfile.gettempdir(), ['tests/test_x_mr.py'], 'unittest', 30)
                self.assertTrue(captured)
                argv, env, parent = captured[0]
                self.assertEqual(env.get('PYTHONWARNINGS'), 'ignore')
                self.assertEqual(parent, 'default')
                self.assertEqual(os.environ.get('PYTHONWARNINGS'), 'default')
                self.assertIn('unittest', argv)
                self.assertIn('-v', argv)
                self.assertEqual(env.get('PATH'), os.environ.get('PATH'))
                captured.clear()
                acceptance.run_named(tempfile.gettempdir(), ['src/a.mr.spec.ts'], 'vitest', 30)
                self.assertTrue(captured)
                v_argv, v_env, v_parent = captured[0]
                self.assertEqual(v_env.get('PYTHONWARNINGS'), 'default')
                self.assertEqual(v_parent, 'default')
                self.assertIn('vitest', v_argv)
                self.assertNotIn('unittest', v_argv)


class TestScopeGaps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mr-mrg1a-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.n = 0

    def patch(self, before, after):
        self.n += 1
        repo = gitfix.make_repo(os.path.join(self.tmp, 'r%d' % self.n), before)
        reset = patches.take_reset_tree(repo)
        for rel, text in after.items():
            _write(repo, rel, text)
        return patches.cumulative_diff(repo, reset)

    def _checklist(self, paths, est):
        return {
            'est_loc': est,
            'scope': {'modify': [], 'create': list(paths), 'forbidden': []},
        }

    def test_loc_excludes_test_files(self):
        code = 'src/app.py'
        tests = 'tests/test_big_mr.py'
        diff = self.patch({}, {code: _lines(50), tests: _lines(400)})
        sig = scope.scope(
            diff, checklist=self._checklist([code, tests], 40),
            difficulty='medium', files_named=(),
        )
        self.assertEqual(sig.loc, 50)
        self.assertNotIn('too large: loc > 2\u00d7est', sig.reasons)
        self.assertTrue(sig.ok, sig.reasons)
        self.assertIn(code, sig.files)
        self.assertIn(tests, sig.files)

        big = 'src/big.py'
        diff2 = self.patch({}, {big: _lines(100)})
        sig2 = scope.scope(
            diff2, checklist=self._checklist([big], 40),
            difficulty='medium', files_named=(),
        )
        self.assertEqual(sig2.loc, 100)
        self.assertFalse(sig2.ok)
        self.assertIn('too large: loc > 2\u00d7est', sig2.reasons)

    def test_test_path_classifier(self):
        cases = [
            ('src/app.py', 10, False),
            ('src/contest.py', 4, False),
            ('src/testing/util.py', 3, False),
            ('src/testutil.py', 2, False),
            ('src/mytest.py', 2, False),
            ('lib/test_foo.ts', 2, False),
            ('lib/foo_test.ts', 2, False),
            ('tests/fixtures/case.json', 6, True),
            ('pkg/test/helper.py', 5, True),
            ('src/__tests__/comp.ts', 5, True),
            ('pkg/test_foo.py', 5, True),
            ('pkg/foo_test.py', 5, True),
            ('web/a.test.ts', 5, True),
            ('web/b.spec.tsx', 5, True),
        ]
        files = {}
        expect = 0
        for path, count, is_test in cases:
            files[path] = _lines(count)
            if not is_test:
                expect += count
        self.assertEqual(expect, 25)
        diff = self.patch({}, files)
        sig = scope.scope(
            diff, checklist=self._checklist([path for path, _, _ in cases], 100),
            difficulty='medium', files_named=(),
        )
        self.assertEqual(sig.loc, 25)
        self.assertNotIn('too large: loc > 2\u00d7est', sig.reasons)
        self.assertTrue(sig.ok, sig.reasons)
        for path, _, _ in cases:
            self.assertIn(path, sig.files)


class TestPromptAndSpec(unittest.TestCase):
    def test_planner_prompts_state_test_file_rule(self):
        phrase = 'any path segment is tests, test, or __tests__'
        markers = ('test_*.py', '*_test.py', '*.test.*', '*.spec.*')
        for rel in ('mr/prompts/planner.unittest.v1.txt', 'mr/prompts/planner.v1.txt'):
            body = _read(rel)
            self.assertIn('outside the test files', body)
            self.assertIn(phrase, body)
            self.assertIn('fixtures', body)
            for marker in markers:
                self.assertIn(marker, body)

class TestPlannerMultiDoc(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = td.name
        self.home = os.path.join(self.dir, 'home')
        self.repo = gitfix.make_repo(os.path.join(self.dir, 'repo'))
        self.task_id = 't-mrg1a01'
        raw = config._deep_merge(defaults(), {
            'paths': {
                'home': self.home,
                'cursor_agent': gitfix.FAKE_CURSOR,
                'claude': gitfix.FAKE_CLAUDE,
                'check': gitfix.FAKE_CHECK,
            },
            'acceptance': {'default': 'unittest'},
        })
        with mock.patch.dict(os.environ, {'MR_TEST_ALLOW_FAKE_PATHS': '1'}):
            self.cfg = config.from_dict(raw)
        self.task = Task(
            id=self.task_id,
            text='add mul',
            repo=self.repo,
            base=gitfix.head(self.repo),
            params=Params(),
            files_named=('README.md',),
            source='cli',
        )

    def _scenario(self, steps):
        path = gitfix.write_scenario(self.dir, {'cursor': {'grok-4.7-high': steps}})
        check = gitfix.write_check(self.dir, {'default': dict(_CHECK_OK)})
        patcher = mock.patch.dict(os.environ, gitfix.fake_env(path, check))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.scenario = path
        return path

    def _plan(self):
        return planner.plan(
            self.task, cfg=self.cfg, tree=self.repo, task_id=self.task_id, attempt=1,
            difficulty='medium',
        )

    def _calls(self):
        with open(self.scenario + '.calls.jsonl', encoding='utf-8') as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def test_first_invalid_then_valid_is_accepted(self):
        good = _unit_doc(_RED, ['mul.py', 'tests/test_mul_mr.py'], 'Add mul.')
        text = json.dumps({'goal': 'partial'}) + '\n' + json.dumps(good)
        self._scenario([{'result': text}])
        result = self._plan()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.checklist['goal'], 'Add mul.')

    def test_two_valid_documents_keep_the_first(self):
        first = _unit_doc(_RED, ['mul.py', 'tests/test_mul_mr.py'], 'First checklist.')
        second = _unit_doc(_RED, ['mul.py', 'tests/test_mul_mr.py'], 'Second checklist.')
        text = json.dumps(first) + '\n' + json.dumps(second)
        self._scenario([{'result': text}])
        result = self._plan()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.checklist['goal'], 'First checklist.')

    def test_no_valid_document_names_the_count(self):
        text = json.dumps({'goal': 'one'}) + '\n' + json.dumps({'goal': 'two'})
        self._scenario([{'result': text}, {'result': text}])
        result = self._plan()
        blob = '\n'.join(result.errors)
        self.assertIn('reply had 2 JSON documents; none valid', blob)
        self.assertIn('keys:', blob)
        self.assertNotIn('not one JSON document', blob)
        calls = self._calls()
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn('reply had 2 JSON documents; none valid', calls[1]['prompt'])
        self.assertIn('The previous checklist was invalid', calls[1]['prompt'])
