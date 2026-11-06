'''AJ8: mr run with the plan judge on: accept, mechanical repair, planner rerun, reject twice, judge failure.'''
import copy
import json
import unittest

from tests.test_loop_run import GH, GX, PLAN, README, REVIEW_PASS, SONNET, LoopCase, mul

TEST_PATH = 'tests/test_mul_mr.py'
EXTRA = 'tests/test_extra_mr.py'
NOTE = 'plan rejected twice; running without a plan'
MEDIUM = ('--type', 'bugfix', '--difficulty', 'medium')
PASSING = ('import unittest\n\n\nclass TestMul(unittest.TestCase):\n'
           '    def test_mul_basic(self):\n        self.assertTrue(True)\n')
TWO = ('import unittest\n\n\nclass TestMul(unittest.TestCase):\n'
       '    def test_mul_basic(self):\n        import mul\n        self.assertEqual(mul.mul(2, 3), 6)\n\n'
       '    def test_mul_zero(self):\n        self.assertTrue(True)\n')
ZERO = TEST_PATH + '::test_mul_zero passed on base'
FIX = 'rewrite test_mul_basic so it calls mul.mul(2, 3) and fails until mul.py exists'
WHY_1 = 'The plan locks a test that already passes; plan again.'
WHY_2 = 'The stronger plan still locks a passing test.'


def verdict(kind, reason, instructions=(), ok=()):
    return {'result': json.dumps({'verdict': kind, 'reason': reason, 'instructions': list(instructions),
                                  'findings_ok': list(ok)})}


def plan_step(content=None, tests=None, modify=()):
    doc = copy.deepcopy(PLAN)
    doc['scope']['modify'] += list(modify)
    if content is not None:
        doc['tests'][0]['content'] = content
    if tests is not None:
        doc['acceptance'][0]['tests'] = list(tests)
    return {'result': json.dumps(doc)}


BAD = plan_step(PASSING)


class JudgeCase(LoopCase):
    def set_scenario(self, scenario, check=None):
        super().set_scenario(scenario, check)
        self.env['MR_HOME'] = self.home

    def run_raw(self):
        code, out, err = self.mr(*MEDIUM)
        return code, (json.loads(out) if out.strip() else {}), out + err

    def judged(self):
        return self.records('check_judge')

    def statuses(self):
        return [r.get('status') for r in self.records('planner')]


class TestPlanJudgeRun(JudgeCase):
    def test_accept_drops_passing_method_and_requires_s_plus_review(self):
        accept = verdict('accept', 'The zero case adds nothing; drop it from the lock.', ok=[ZERO])
        self.set_scenario({'cursor': {GH: [plan_step(TWO, ['test_mul_basic', 'test_mul_zero'])], GX: [mul('*')]},
                           'claude': {SONNET: [accept, {'result': REVIEW_PASS}]}})
        code, doc, text = self.run_raw()
        self.assertEqual(code, 0, text)
        self.assertEqual(self.models()[:3], [GH, GH, SONNET])
        judged = self.judged()
        self.assertEqual(len(judged), 1, judged)
        rec = judged[0]
        self.assertEqual((rec.get('check'), rec.get('verdict'), rec.get('status')), ('plan', 'accept', 'ok'))
        self.assertEqual((rec.get('rung'), rec.get('judge_model'), rec.get('deferred')), ('S', SONNET, False))
        self.assertIn(ZERO, rec.get('findings') or [])
        argv = [c['argv'] for c in self.calls() if c['model'] == SONNET][0]
        self.assertEqual(argv[argv.index('--effort') + 1], 'high')
        for part in ('mr.check.v1', ZERO, 'Add mul.'):
            self.assertIn(part, self.prompts(SONNET)[0])
        self.assertEqual(self.statuses(), ['let_through'])
        self.assertTrue(rec.get('ref'))
        self.assertEqual(self.records('planner')[0].get('plan_judge_ref'), rec.get('ref'))
        st = self.state_of(doc['task'])
        self.assertEqual(st['checklist']['acceptance'][0]['tests'], ['test_mul_basic'])
        self.assertEqual(st['claude_rungs'], 0)
        self.assertEqual([r['rung'] for r in self.records('reviewer')], ['S+'])
        self.assert_schema()

    def test_mechanical_repair_is_applied_with_one_judge_call(self):
        move = verdict('repair', 'The new test file belongs in scope.create.', ['move ' + EXTRA + ' to create'])
        self.set_scenario({'cursor': {GH: [plan_step(modify=[EXTRA])], GX: [mul('*')]},
                           'claude': {SONNET: [move, {'result': REVIEW_PASS}]}})
        code, doc, text = self.run_raw()
        self.assertEqual(code, 0, text)
        judged = self.judged()
        self.assertEqual([r.get('verdict') for r in judged], ['repair'])
        self.assertTrue(judged[0].get('ref'))
        self.assertEqual(self.models().count(GH), 2)
        self.assertEqual(self.statuses(), ['repaired'])
        self.assertEqual(self.records('planner')[0].get('plan_judge_ref'), judged[0].get('ref'))
        scope = self.state_of(doc['task'])['checklist']['scope']
        self.assertIn(EXTRA, scope['create'])
        self.assertNotIn(EXTRA, scope['modify'])
        self.assert_schema()

    def test_repair_needing_new_tests_reruns_planner_then_judge_cap(self):
        fix = verdict('repair', 'The locked test must fail before mul exists.', [FIX])
        self.set_scenario({'cursor': {GH: [BAD], GX: [{'files': README}]},
                           'claude': {SONNET: [fix, verdict('reject', WHY_2), BAD, BAD, {'result': REVIEW_PASS}]}})
        code, doc, text = self.run_raw()
        self.assertEqual(code, 0, text)
        self.assertEqual([r.get('verdict') for r in self.judged()], ['repair', 'reject'])
        self.assertTrue(any(FIX in p for p in self.prompts(GH)[2:]), 'the planner reran with the instruction')
        self.assertIn(WHY_2, self.prompts(SONNET)[2])
        self.assertEqual([r['rung'] for r in self.records('planner')][-1], 'S')
        self.assertIsNone(self.state_of(doc['task'])['checklist'])
        self.assertEqual([r['rung'] for r in self.records('reviewer')], ['S+'])
        self.assertIn(NOTE, text)
        self.assert_schema()

    def test_reject_twice_runs_plan_less_with_s_plus_review(self):
        self.set_scenario({'cursor': {GH: [BAD], GX: [{'files': README}]},
                           'claude': {SONNET: [verdict('reject', WHY_1), BAD, BAD, verdict('reject', WHY_2),
                                               {'result': REVIEW_PASS}]}})
        code, doc, text = self.run_raw()
        self.assertNotEqual(code, 3, text)
        self.assertEqual([r.get('verdict') for r in self.judged()], ['reject', 'reject'])
        self.assertEqual(code, 0, text)
        self.assertEqual([r['rung'] for r in self.records('planner')], ['G-h', 'S'])
        self.assertEqual(self.statuses(), ['rejected', 'rejected'])
        self.assertIn(WHY_1, self.prompts(SONNET)[1])
        self.assertIsNone(self.state_of(doc['task'])['checklist'])
        self.assertEqual([r['rung'] for r in self.records('reviewer')], ['S+'])
        self.assertIs(self.only_task().get('plan_rejected'), True)
        self.assertIn(NOTE, text)
        self.assertIn(WHY_2, text)
        self.assert_schema()

    def test_judge_invalid_twice_falls_back_to_stronger_plan(self):
        self.set_scenario({'cursor': {GH: [BAD], GX: [mul('*')]},
                           'claude': {SONNET: [{'result': 'not json'}, {'result': '{"verdict": "maybe"}'},
                                               {'result': json.dumps(PLAN)}, {'result': REVIEW_PASS}]}})
        code, doc, text = self.run_raw()
        judged = self.judged()
        self.assertTrue(judged, text)
        self.assertEqual(judged[-1].get('status'), 'invalid')
        self.assertIsNone(judged[-1].get('verdict'))
        self.assertNotIn('ok', [r.get('status') for r in judged])
        self.assertEqual(code, 0, text)
        self.assertEqual(self.models()[:5], [GH, GH, SONNET, SONNET, SONNET])
        self.assertEqual([r['rung'] for r in self.records('planner')], ['G-h', 'S'])
        self.assertEqual(self.statuses()[-1], 'valid')
        self.assertEqual(self.state_of(doc['task'])['checklist']['goal'], 'Add mul.')
        self.assert_schema()


if __name__ == '__main__':
    unittest.main()
