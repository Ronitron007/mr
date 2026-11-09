'''gate.acceptance_health: signal A needs a healthy locked-test run, and a plan needs requirements and tests.'''
import copy
import json
import os
import shutil
import tempfile
import unittest

from mr import acceptance, config, planner
from mr.defaults import defaults
from tests import gitfix
from tests.test_aj10_defer_mr import ALLOW, DENY, DeferCase, verdict
from tests.test_loop_run import GH, GX, PLAN, REVIEW_PASS, SONNET, TEST_PATH, LoopCase

TEARDOWN_RED = ('import mul\nimport unittest\n\n\nclass TestMul(unittest.TestCase):\n'
                '    def test_mul_basic(self):\n        self.assertEqual(mul.mul(2, 3), 6)\n\n\n'
                'def tearDownModule():\n    mul.close()\n')
MEDIUM = ('--type', 'bugfix', '--difficulty', 'medium')
STRAY = 'sub/other.txt'


def mul_close(close_body, note='', extra=None):
    files = {'mul.py': f'def mul(a, b):\n    return a * b{note}\n\n\ndef close():\n    {close_body}\n'}
    return {'files': {**files, **(extra or {})}}


BROKEN = mul_close('raise RuntimeError("cleanup failed")')
FIXED = mul_close('return None', '  # fixed')


def teardown_plan():
    doc = copy.deepcopy(PLAN)
    doc['tests'][0]['content'] = TEARDOWN_RED
    return {'result': json.dumps(doc)}


class TestCodexReproductions(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='mr-gate-acc-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = os.path.join(self.root, 'repo')
        gitfix.make_repo(self.repo, {'tests/__init__.py': ''}, branch='feature/x')
        raw = config._deep_merge(defaults(), {'paths': {'home': os.path.join(self.root, 'home')},
                                              'acceptance': {'runners': {'repo': 'unittest'}}})
        self.cfg = config.from_dict(raw)

    def test_suite_failure_after_named_tests_pass_fails_a(self):
        rel = 'tests/test_demo_mr.py'
        with open(os.path.join(self.repo, rel), 'w', encoding='utf-8') as fh:
            fh.write('import unittest\nclass TestDemo(unittest.TestCase):\n'
                     '    def test_demo(self):\n        self.assertTrue(True)\n'
                     'def tearDownModule():\n    raise RuntimeError("suite cleanup failed")\n')
        doc = {'acceptance': [{'id': 'A1', 'covers': ['R1'], 'file': rel, 'tests': ['test_demo']}]}
        lockdoc = acceptance.lock(self.repo, doc, self.cfg, 'repo', task_id='t-lock')
        sig = acceptance.acceptance(self.repo, doc, lockdoc, cfg=self.cfg, repo_name='repo')
        self.assertFalse(sig.ok, sig)
        self.assertEqual(sig.passed, ((rel, 'test_demo'),))
        self.assertIn(f'{rel}::tearDownModule', sig.detail)

    def test_empty_checklist_is_invalid_and_never_passes_a(self):
        empty = {'goal': 'do work', 'scope': {'modify': ['README.md'], 'create': [], 'forbidden': []},
                 'requirements': [], 'acceptance': [], 'est_loc': 10, 'files_map': {}, 'tests': []}
        errors = planner.validate(empty, repo_root=self.repo, runner='unittest')
        for key in ('requirements', 'acceptance', 'tests'):
            self.assertTrue(any(key in err and 'non-empty' in err for err in errors), (key, errors))
        self.assertFalse(acceptance.signal_from_statuses(empty, {}).ok)

    def test_acceptance_item_without_test_names_is_invalid(self):
        doc = copy.deepcopy(PLAN)
        doc['acceptance'][0]['tests'] = []
        errors = planner.validate(doc, repo_root=self.repo, runner='unittest')
        self.assertIn('keys: acceptance.tests must be a non-empty list of strings', errors)


class TestTeardownErrorEndToEnd(LoopCase):
    def run_medium(self):
        code, out, err = self.mr(*MEDIUM)
        return code, (json.loads(out) if out.strip() else {}), out + err

    def test_named_test_passes_but_teardown_fails_is_not_accepted(self):
        self.set_scenario({'cursor': {GH: [teardown_plan()], GX: [BROKEN, {'result': REVIEW_PASS}]},
                           'claude': {SONNET: [FIXED]}})
        code, doc, text = self.run_medium()
        self.assertEqual(code, 0, text)
        st = self.state_of(doc['task'])
        self.assertEqual([(r['rung'], r['class']) for r in st['rungs']], [('G-x', 'tried_wrong'), ('S', None)])
        self.assertIs(st['rungs'][0]['signals']['A'], False)
        self.assertIn(f'{TEST_PATH}::tearDownModule', st['rungs'][0]['failing'])
        self.assertIn(f'{TEST_PATH}::tearDownModule', self.prompts(SONNET)[0], 'the handoff names the teardown')
        self.assertIs(st['rungs'][1]['signals']['A'], True)
        self.assert_schema()

    def test_teardown_error_on_every_rung_never_accepts(self):
        self.set_scenario({'cursor': {GH: [teardown_plan()], GX: [BROKEN]},
                           'claude': {SONNET: [mul_close('raise RuntimeError("still broken")', '  # s')]}})
        code, doc, text = self.run_medium()
        self.assertNotEqual(code, 0, text)
        st = self.state_of(doc['task'])
        self.assertNotEqual(st['outcome'], 'accepted', st)
        self.assertTrue(all(r['signals']['A'] is not True for r in st['rungs']), st['rungs'])
        self.assertEqual([r['class'] for r in st['rungs']][:2], ['tried_wrong', 'spec_suspect'],
                         'two models fail the same locked teardown: the plan is suspect, not a third retry')
        self.assertEqual(self.records('reviewer'), [], 'no review for a red A')


class TestResumeWithChecklist(DeferCase):
    def test_resumed_scope_reject_classifies_with_a_checklist(self):
        self.set_scenario({'cursor': {GH: [teardown_plan()],
                                      GX: [mul_close('return None', extra={STRAY: 'x\n'}), {'result': REVIEW_PASS}]},
                           'claude': {SONNET: [verdict('reject', 'The stray file is not part of the task.'),
                                               FIXED]}}, check=DENY)
        code, doc, text = self.go(*MEDIUM)
        self.assertEqual(code, 75, text)
        self.guard(ALLOW)
        code, out, text = self.cli('--json', 'resume', doc['task'])
        self.assertEqual(code, 0, text)
        st = self.state_of(doc['task'])
        self.assertEqual([(r['rung'], r['class']) for r in st['rungs']], [('G-x', 'scope_rejected'), ('S', None)])
        self.assertEqual(st['rungs'][0]['failing'], [])


if __name__ == '__main__':
    unittest.main()
