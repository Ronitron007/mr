'''AJ7: planner auto_repair and the plan() status fields (spec 8A.3 steps 1-2, 9.1 planner records).'''
import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from mr import config, planner
from mr.defaults import defaults
from mr.task import Params, make_task
from tests import gitfix

TEST = 'tests/test_x_mr.py'
CONTENT = ('import unittest\n\n\nclass TestX(unittest.TestCase):\n'
           '    def test_a(self):\n        self.assertTrue(False)\n')
PLAN_TEST = 'tests/test_mul_mr.py'
RED = ('import mul\nimport unittest\n\n\nclass TestMul(unittest.TestCase):\n'
       '    def test_mul_basic(self):\n        self.assertEqual(mul.mul(2, 3), 6)\n')
PASSING = ('import unittest\n\n\nclass TestMul(unittest.TestCase):\n'
           '    def test_mul_basic(self):\n        self.assertTrue(True)\n')
CHECK_OK = {
    'hour_usage': 10, 'limit': 40, 'unit': 'points', 'exceeded': False,
    'five_hour_pct': 30.0, 'five_hour_resets_at': '2026-10-30T05:00:00Z',
    'seven_day_pct': 12.0, 'sample_age_s': 30, 'samples_in_hour': 3,
    'has_baseline': True, 'checked_at': '2026-10-30T00:00:00Z',
}


def req(rid, text='x SHALL work'):
    return {'id': rid, 'text': text, 'examples': [{'given': 'g', 'when': 'w', 'then': 't'}]}


def base_doc():
    return {
        'goal': 'Add new.',
        'scope': {'modify': ['src/app.py'], 'create': ['src/new.py', TEST], 'forbidden': ['package.json']},
        'requirements': [req('R1')],
        'acceptance': [{'id': 'A1', 'covers': ['R1'], 'file': TEST, 'tests': ['test_a']}],
        'est_loc': 10,
        'files_map': {'R1': ['src/app.py', 'src/new.py']},
        'tests': [{'path': TEST, 'content': CONTENT}],
    }


def unit_doc(content, extra_create=()):
    return {
        'goal': 'Add mul.',
        'scope': {'modify': ['README.md'], 'create': ['mul.py', PLAN_TEST] + list(extra_create),
                  'forbidden': ['package.json']},
        'requirements': [req('R1', 'mul SHALL multiply')],
        'acceptance': [{'id': 'A1', 'covers': ['R1'], 'file': PLAN_TEST, 'tests': ['test_mul_basic']}],
        'est_loc': 10,
        'files_map': {'R1': ['mul.py']},
        'tests': [{'path': PLAN_TEST, 'content': content}],
    }


class TestAutoRepair(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = os.path.realpath(td.name)
        self.home = os.path.join(root, 'home')
        os.makedirs(self.home)
        patcher = mock.patch.dict(os.environ, {'MR_HOME': self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.repo = gitfix.make_repo(os.path.join(root, 'repo'), {'src/app.py': 'X = 1\n'})

    def repair(self, doc):
        fn = getattr(planner, 'auto_repair', None)
        self.assertTrue(callable(fn), 'mr.planner.auto_repair is missing')
        out = fn(doc, repo_root=self.repo, runner='unittest')
        self.assertIsInstance(out, tuple)
        self.assertEqual(len(out), 2)
        fixed, repairs = out
        self.assertIsInstance(fixed, dict)
        self.assertIsInstance(repairs, list)
        for entry in repairs:
            self.assertIsInstance(entry, dict, repairs)
            self.assertIsInstance(entry.get('kind'), str, entry)
            self.assertIn('detail', entry, entry)
            self.assertTrue('path' in entry or 'id' in entry, entry)
        return fixed, repairs

    def valid(self, doc):
        self.assertEqual(planner.validate(doc, repo_root=self.repo, runner='unittest'), [])

    def test_create_path_that_exists_moves_to_modify(self):
        doc = base_doc()
        doc['scope']['create'].append('README.md')
        fixed, repairs = self.repair(doc)
        self.assertIn('README.md', fixed['scope']['modify'])
        self.assertNotIn('README.md', fixed['scope']['create'])
        self.assertIn('README.md', [e.get('path') for e in repairs], repairs)
        self.valid(fixed)

    def test_missing_modify_path_moves_to_create(self):
        doc = base_doc()
        doc['scope']['modify'].append('src/later.py')
        fixed, repairs = self.repair(doc)
        self.assertIn('src/later.py', fixed['scope']['create'])
        self.assertNotIn('src/later.py', fixed['scope']['modify'])
        self.assertIn('src/later.py', [e.get('path') for e in repairs], repairs)
        self.valid(fixed)

    def test_dot_and_absolute_paths_become_repo_relative(self):
        doc = base_doc()
        doc['scope']['modify'] = [os.path.join(self.repo, 'src', 'app.py')]
        doc['scope']['create'] = ['./src/new.py', TEST]
        fixed, repairs = self.repair(doc)
        self.assertEqual(fixed['scope']['modify'], ['src/app.py'])
        self.assertEqual(fixed['scope']['create'], ['src/new.py', TEST])
        self.assertGreaterEqual(len(repairs), 2, repairs)
        self.valid(fixed)

    def test_test_path_missing_from_create_is_added(self):
        doc = base_doc()
        doc['scope']['create'] = ['src/new.py']
        fixed, repairs = self.repair(doc)
        self.assertIn(TEST, fixed['scope']['create'])
        self.assertIn(TEST, [e.get('path') for e in repairs], repairs)
        self.valid(fixed)

    def test_files_map_path_outside_scope_is_added_to_modify(self):
        doc = base_doc()
        doc['files_map']['R1'].append('README.md')
        fixed, repairs = self.repair(doc)
        self.assertIn('README.md', fixed['scope']['modify'])
        self.assertIn('README.md', [e.get('path') for e in repairs], repairs)
        self.valid(fixed)

    def test_ids_renumbered_with_references(self):
        doc = base_doc()
        doc['requirements'] = [req('R2', 'first SHALL work'), req('R3', 'second SHALL work')]
        doc['acceptance'][0]['id'] = 'A2'
        doc['acceptance'][0]['covers'] = ['R2', 'R3']
        doc['files_map'] = {'R2': ['src/app.py'], 'R3': ['src/new.py']}
        fixed, repairs = self.repair(doc)
        self.assertEqual([r['id'] for r in fixed['requirements']], ['R1', 'R2'])
        self.assertEqual([r['text'] for r in fixed['requirements']], ['first SHALL work', 'second SHALL work'])
        self.assertEqual(fixed['acceptance'][0]['id'], 'A1')
        self.assertEqual(fixed['acceptance'][0]['covers'], ['R1', 'R2'])
        self.assertEqual(fixed['files_map'], {'R1': ['src/app.py'], 'R2': ['src/new.py']})
        self.assertTrue(any('id' in e for e in repairs), repairs)
        self.valid(fixed)

    def test_never_edits_test_content_or_test_paths(self):
        doc = base_doc()
        doc['scope']['create'] = ['src/new.py', 'README.md']
        doc['scope']['modify'] = ['./src/app.py']
        tests_before = copy.deepcopy(doc['tests'])
        acc_file = doc['acceptance'][0]['file']
        fixed, repairs = self.repair(doc)
        self.assertTrue(repairs)
        self.assertEqual(fixed['tests'], tests_before)
        self.assertEqual(fixed['acceptance'][0]['file'], acc_file)
        self.assertFalse(os.path.exists(os.path.join(self.repo, TEST)), 'auto_repair must not write files')
        self.valid(fixed)

    def test_idempotent_pure_and_safe_on_bad_input(self):
        doc = base_doc()
        doc['scope']['create'] = ['src/new.py', 'README.md']
        doc['scope']['modify'] = ['./src/app.py', 'src/later.py']
        original = copy.deepcopy(doc)
        once, first = self.repair(doc)
        self.assertEqual(doc, original, 'auto_repair must not change its input')
        self.assertTrue(first)
        twice, second = self.repair(once)
        self.assertEqual(twice, once)
        self.assertEqual(second, [])
        same, none = self.repair(base_doc())
        self.assertEqual((same, none), (base_doc(), []))
        for bad in ({'goal': 'one'}, {'goal': 'x', 'scope': 'nope', 'tests': None}):
            fixed, repairs = self.repair(bad)
            self.assertEqual(repairs, [], bad)


class TestPlanStatus(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = os.path.realpath(td.name)
        self.home = os.path.join(self.dir, 'home')
        self.repo = gitfix.make_repo(os.path.join(self.dir, 'repo'))
        self.task_id = 't-aj7plan'
        raw = config._deep_merge(defaults(), {
            'paths': {'home': self.home, 'cursor_agent': gitfix.FAKE_CURSOR,
                      'claude': gitfix.FAKE_CLAUDE, 'check': gitfix.FAKE_CHECK},
            'acceptance': {'default': 'unittest'},
        })
        with mock.patch.dict(os.environ, {'MR_TEST_ALLOW_FAKE_PATHS': '1', 'MR_HOME': self.home}):
            self.cfg = config.from_dict(raw)
        self.task = make_task('add mul', self.repo, gitfix.head(self.repo), Params(), files_named=('README.md',))
        self.scenario = None

    def _scenario(self, steps):
        sdir = tempfile.mkdtemp(dir=self.dir)
        path = gitfix.write_scenario(sdir, {'cursor': {'grok-4.7-high': steps}})
        check = gitfix.write_check(sdir, {'default': dict(CHECK_OK)})
        env = dict(gitfix.fake_env(path, check), MR_HOME=self.home)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.scenario = path

    def _calls(self):
        try:
            with open(self.scenario + '.calls.jsonl', encoding='utf-8') as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except FileNotFoundError:
            return []

    def _plan(self):
        return planner.plan(self.task, cfg=self.cfg, tree=self.repo, task_id=self.task_id, attempt=1,
                            difficulty='medium')

    def test_create_path_that_exists_is_repaired_without_a_second_call(self):
        self._scenario([{'result': json.dumps(unit_doc(RED, ['.gitignore']))}])
        result = self._plan()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(getattr(result, 'status', None), 'repaired')
        repairs = getattr(result, 'repairs', None)
        self.assertTrue(repairs, 'repairs must be logged')
        self.assertIn('.gitignore', json.dumps(repairs))
        self.assertEqual(len(self._calls()), 1)
        self.assertIn('.gitignore', result.checklist['scope']['modify'])
        self.assertNotIn('.gitignore', result.checklist['scope']['create'])

    def test_valid_plan_status_and_empty_lists(self):
        self._scenario([{'result': json.dumps(unit_doc(RED))}])
        result = self._plan()
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(getattr(result, 'status', None), 'valid')
        self.assertEqual(getattr(result, 'repairs', None), [])
        self.assertEqual(getattr(result, 'retry_errors', None), [])
        self.assertIs(getattr(result, 'infra', None), False)

    def test_rejected_plan_keeps_retry_errors(self):
        self._scenario([{'result': json.dumps(unit_doc(PASSING))}])
        result = self._plan()
        self.assertFalse(result.ok)
        self.assertEqual(getattr(result, 'status', None), 'rejected')
        self.assertIs(getattr(result, 'infra', None), False)
        retry = getattr(result, 'retry_errors', None)
        self.assertIsInstance(retry, list)
        self.assertTrue(any('passed on base' in err for err in retry), retry)
        self.assertEqual(len(self._calls()), 2)

    def test_timeout_or_no_json_twice_is_infra(self):
        self._scenario([{'sleep_s': 100000}])
        with mock.patch.dict(os.environ, {'MR_TEST_TIME_SCALE': '0.0005'}):
            result = self._plan()
        self.assertFalse(result.ok)
        self.assertIs(getattr(result, 'infra', None), True, 'two planner timeouts are infra')
        self.assertEqual(getattr(result, 'status', None), 'rejected')
        self._scenario([{'result': 'not json'}])
        result = self._plan()
        self.assertFalse(result.ok)
        self.assertIs(getattr(result, 'infra', None), True, 'two replies with no JSON document are infra')


if __name__ == '__main__':
    unittest.main()
