'''AJ7: mr run with plan.judge on: stronger re-plan, plan-less run with an S+ review, planner infra defers (75).'''
import copy
import inspect
import json
import os
import tempfile
import unittest
from unittest import mock

from mr import config, loop
from mr.config import ConfigError
from mr.defaults import defaults
from tests import gitfix
from tests.test_loop_run import GH, GX, PLAN, README, REVIEW_PASS, SONNET, LoopCase, mul

PASSING = ('import unittest\n\n\nclass TestMul(unittest.TestCase):\n'
           '    def test_mul_basic(self):\n        self.assertTrue(True)\n')
NOTE = 'plan rejected twice; running without a plan'
MEDIUM = ('--type', 'bugfix', '--difficulty', 'medium')


def plan_doc(content=None, extra_create=()):
    doc = copy.deepcopy(PLAN)
    doc['scope']['create'] += list(extra_create)
    if content is not None:
        doc['tests'][0]['content'] = content
    return json.dumps(doc)


class PlanCase(LoopCase):
    plan_cfg = None

    def write_cfg(self):
        super().write_cfg()
        if self.plan_cfg is None:
            return
        with open(self.cfgpath, encoding='utf-8') as fh:
            raw = json.load(fh)
        raw['plan'] = dict(self.plan_cfg)
        with open(self.cfgpath, 'w', encoding='utf-8') as fh:
            json.dump(raw, fh)

    def set_scenario(self, scenario, check=None):
        super().set_scenario(scenario, check)
        self.env['MR_HOME'] = self.home

    def run_raw(self):
        code, out, err = self.mr(*MEDIUM)
        return code, (json.loads(out) if out.strip() else {}), out + err

    def statuses(self):
        return [r.get('status') for r in self.records('planner')]


class TestPlanJudgeOn(PlanCase):
    def test_repaired_plan_runs_after_one_planner_call(self):
        self.set_scenario({'cursor': {GH: [{'result': plan_doc(extra_create=['.gitignore'])}], GX: [mul('*')]},
                           'claude': {SONNET: [{'result': REVIEW_PASS}]}})
        code, doc, text = self.run_raw()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.models().count(GH), 1)
        rows = self.records('planner')
        self.assertEqual(self.statuses(), ['repaired'])
        self.assertIn('.gitignore', json.dumps(rows[0].get('repairs')), rows[0])
        self.assertEqual(rows[0].get('retry_errors'), [])
        self.assertIn('.gitignore', self.state_of(doc['task'])['checklist']['scope']['modify'])
        self.assert_schema()

    def test_two_invalid_plans_then_one_stronger_plan(self):
        self.set_scenario({'cursor': {GH: [{'result': plan_doc(PASSING)}], GX: [mul('*')]},
                           'claude': {SONNET: [{'result': json.dumps(PLAN)}, {'result': REVIEW_PASS}]}})
        code, doc, text = self.run_raw()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.models()[:3], [GH, GH, SONNET])
        self.assertIn('passed on base', self.prompts(SONNET)[0])
        rows = self.records('planner')
        self.assertEqual(self.statuses(), ['rejected', 'valid'])
        self.assertEqual([r['rung'] for r in rows], ['G-h', 'S'])
        self.assertNotIn('invalid_plan', [r.get('failure_class') for r in rows])
        self.assertEqual(self.only_task()['attempts']['plan'], 3)
        self.assertEqual(self.state_of(doc['task'])['checklist']['goal'], 'Add mul.')
        self.assert_schema()

    def test_third_invalid_plan_runs_without_a_plan_and_s_plus_review(self):
        bad = plan_doc(PASSING)
        self.set_scenario({'cursor': {GH: [{'result': bad}], GX: [{'files': README}]},
                           'claude': {SONNET: [{'result': bad}, {'result': REVIEW_PASS}]}})
        code, doc, text = self.run_raw()
        self.assertNotEqual(code, 3, text)
        self.assertEqual(code, 0, text)
        self.assertIsNone(self.state_of(doc['task'])['checklist'])
        self.assertEqual(self.statuses(), ['rejected', 'rejected'])
        self.assertNotIn('invalid_plan', [r.get('failure_class') for r in self.records('planner')])
        self.assertEqual([r['rung'] for r in self.records('reviewer')], ['S+'])
        task = self.only_task()
        self.assertIs(task.get('plan_rejected'), True, task)
        self.assertIn(NOTE, text)
        self.assert_schema()

    def test_planner_infra_twice_defers_with_state_saved(self):
        self.set_scenario({'cursor': {GH: [{'sleep_s': 100000}]}, 'claude': {SONNET: [{'result': 'not json'}]}})
        self.env['MR_TEST_TIME_SCALE'] = '0.002'
        code, doc, text = self.run_raw()
        self.assertEqual(code, 75, text)
        self.assertEqual(doc['outcome'], 'deferred')
        self.assertIn('planner unavailable', doc['reason'])
        models = self.models()
        self.assertIn(GH, models)
        self.assertIn(SONNET, models)
        self.assertNotIn(GX, models)
        st = self.state_of(doc['task'])
        self.assertIsNotNone(st)
        self.assertEqual((st['outcome'], st['exit']), ('deferred', 75))
        self.assertEqual(self.only_task()['exit'], 75)

    def test_plan_findings_hook_exists(self):
        fn = getattr(loop, '_plan_findings', None)
        self.assertTrue(callable(fn), 'mr.loop._plan_findings(c, res) is the AJ8 hook')
        self.assertEqual(list(inspect.signature(fn).parameters), ['c', 'res'])


class TestPlanJudgeOff(PlanCase):
    plan_cfg = {'judge': 'off'}

    def test_plan_judge_off_keeps_exit_3(self):
        self.set_scenario({'cursor': {GH: [{'result': plan_doc(PASSING)}]}})
        code, doc, text = self.run_raw()
        self.assertEqual(code, 3, text)
        self.assertEqual(self.models(), [GH, GH])
        self.assertEqual(doc['outcome'], 'needs_human')
        rows = self.records('planner')
        self.assertEqual([r.get('failure_class') for r in rows], ['invalid_plan'])
        self.assertEqual([r.get('status') for r in rows], ['rejected'])
        self.assertEqual(self.only_task()['exit'], 3)


class TestPlanJudgeConfig(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.home = os.path.join(os.path.realpath(td.name), 'home')
        patcher = mock.patch.dict(os.environ, {'MR_HOME': self.home, 'MR_TEST_ALLOW_FAKE_PATHS': '1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def raw(self, plan):
        return config._deep_merge(defaults(), {
            'paths': {'home': self.home, 'cursor_agent': gitfix.FAKE_CURSOR,
                      'claude': gitfix.FAKE_CLAUDE, 'check': gitfix.FAKE_CHECK},
            'plan': plan})

    def test_default_plan_judge_is_on(self):
        block = defaults().get('plan')
        self.assertIsInstance(block, dict, 'DEFAULT_YAML needs a top-level plan block')
        self.assertIn(block.get('judge'), ('on', True))

    def test_plan_judge_value_is_validated(self):
        with self.assertRaises(ConfigError):
            config.from_dict(self.raw({'judge': 'maybe'}))
        config.from_dict(self.raw({'judge': 'off'}))
        config.from_dict(self.raw({'judge': 'on'}))


if __name__ == '__main__':
    unittest.main()
