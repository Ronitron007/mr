'''AJ9: mr run with the scope judge on: safety violations, accept / repair / reject, H and A before the judge,
the S+ judge for a Sonnet author, a judge that cannot answer and the per-task cap.'''
import json
import os
import unittest

from tests.test_loop_run import GH, GL, GM, GX, PLAN, README, RED, REVIEW_PASS, SONNET, TEST_PATH, LoopCase, mul

EASY = ('--type', 'docs', '--difficulty', 'easy')
MEDIUM = ('--type', 'bugfix', '--difficulty', 'medium')
STRAY = 'sub/other.txt'
STRAYED = {'files': {STRAY: 'x\n', **README}}
AGAIN = {'files': {'README.md': '# test repo\nfixed typo, again\n'}}
OUT = 'out of scope: ' + STRAY
HELPER = 'tools/helper.py'
UNAVAILABLE = 'scope judge unavailable'
RESET = 'was reset to the start of the task'
KEPT = "still holds the previous rung's changes"
FIX = 'Delete sub/other.txt; the task only needs README.md.'
WHY_REPAIR = 'The README fix is right but a stray file came along.'
WHY_REJECT = 'The stray file has nothing to do with the task.'
WHY_ACCEPT = 'The extra file is harmless for this task.'
FAILING = ('import unittest\n\n\nclass TestSanity(unittest.TestCase):\n'
           '    def test_ok(self):\n        self.assertTrue(False)\n')
PLAN_STEP = {'result': json.dumps(PLAN)}


def verdict(kind, reason, instructions=()):
    return {'result': json.dumps({'verdict': kind, 'reason': reason, 'instructions': list(instructions),
                                  'findings_ok': []})}


def with_files(step, extra):
    return {'files': {**step['files'], **extra}}


class ScopeCase(LoopCase):
    check_judge = {'scope': 'on'}

    def write_cfg(self):
        super().write_cfg()
        with open(self.cfgpath, encoding='utf-8') as fh:
            raw = json.load(fh)
        raw['check_judge'] = dict(self.check_judge)
        with open(self.cfgpath, 'w', encoding='utf-8') as fh:
            json.dump(raw, fh)

    def set_scenario(self, scenario, check=None):
        super().set_scenario(scenario, check)
        self.env['MR_HOME'] = self.home

    def go(self, *args):
        code, out, err = self.mr(*args)
        doc = json.loads(out) if out.strip() else {}
        self.assertIn('task', doc, out + err)
        return code, doc, out + err

    def classes(self, doc):
        return [(r['rung'], r['class']) for r in self.state_of(doc['task'])['rungs']]

    def judged(self):
        return [r for r in self.records('check_judge') if r.get('check') == 'scope']

    def efforts(self, model):
        return [c['argv'][c['argv'].index('--effort') + 1] for c in self.calls() if c['model'] == model]

    def sonnet_calls(self):
        return [c for c in self.calls() if c['model'] == SONNET]

    def exists(self, rel):
        return os.path.exists(os.path.join(self.repo, rel))


class TestSafetyViolation(ScopeCase):
    def test_locked_test_edit_is_a_safety_violation_without_a_judge(self):
        edit = with_files(mul('*'), {TEST_PATH: RED + '# edited\n'})
        self.set_scenario({'cursor': {GH: [PLAN_STEP], GX: [edit, {'result': REVIEW_PASS}]},
                           'claude': {SONNET: [mul('*', '  # s')]}})
        code, doc, text = self.go(*MEDIUM)
        self.assertEqual(self.classes(doc), [('G-x', 'safety_violation'), ('S', None)], text)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.judged(), [])
        handoff = self.prompts(SONNET)[0]
        for part in ('class safety_violation', 'locked test edited: ' + TEST_PATH, RESET):
            self.assertIn(part, handoff, part)
        self.assertEqual([r['signals']['D'] for r in self.records('implementer')], ['violation', 'pass'])
        with open(os.path.join(self.repo, TEST_PATH), encoding='utf-8') as fh:
            self.assertEqual(fh.read(), RED)

    def test_sensitive_file_outside_the_plan_is_a_safety_violation(self):
        leak = with_files(mul('*'), {'.env.local': 'API_URL=http://localhost\n'})
        self.set_scenario({'cursor': {GH: [PLAN_STEP], GX: [leak, {'result': REVIEW_PASS}]},
                           'claude': {SONNET: [mul('*', '  # s')]}})
        code, doc, text = self.go(*MEDIUM)
        self.assertEqual(self.classes(doc), [('G-x', 'safety_violation'), ('S', None)], text)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.judged(), [])
        self.assertIn('sensitive path: .env.local', self.prompts(SONNET)[0])


class TestScopeJudge(ScopeCase):
    def test_accept_counts_d_as_passed_and_forces_review(self):
        self.set_scenario({'cursor': {GL: [STRAYED]},
                           'claude': {SONNET: [verdict('accept', WHY_ACCEPT), {'result': REVIEW_PASS}]}})
        code, doc, text = self.go(*EASY)
        self.assertEqual(self.classes(doc), [('G-l', None)], text)
        self.assertEqual(code, 0, text)
        judged = self.judged()
        self.assertEqual(len(judged), 1, judged)
        rec = judged[0]
        self.assertEqual((rec['verdict'], rec['status'], rec['rung'], rec['judge_model']),
                         ('accept', 'ok', 'S', SONNET))
        self.assertIn(OUT, rec['findings'])
        self.assertEqual(self.efforts(SONNET)[0], 'high')
        prompt = self.prompts(SONNET)[0]
        for part in ('mr.check.v1', '"check": "scope"', OUT, 'fix typo in README.md'):
            self.assertIn(part, prompt, part)
        self.assertEqual(len(self.records('reviewer')), 1, 'a judge-accepted attempt always gets review C')
        impl = self.records('implementer')
        self.assertEqual(impl[0]['signals']['D'], 'flagged')
        self.assertEqual(impl[0].get('d_judge_ref'), rec['ref'])
        self.assertTrue(self.exists(STRAY))
        self.assert_schema()

    def test_sonnet_author_gets_the_s_plus_judge_at_xhigh(self):
        work = with_files(mul('*'), {HELPER: 'X = 1\n'})
        self.set_scenario({'cursor': {GH: [PLAN_STEP], GX: [{'result': REVIEW_PASS}]},
                           'claude': {SONNET: [work, verdict('accept', WHY_ACCEPT)]}})
        code, doc, text = self.go(*MEDIUM, '--rung', 'S')
        self.assertEqual(self.classes(doc), [('S', None)], text)
        self.assertEqual(code, 0, text)
        self.assertEqual([r['rung'] for r in self.judged()], ['S+'])
        self.assertEqual(self.efforts(SONNET)[1], 'xhigh')

    def test_forbidden_glob_goes_to_the_judge(self):
        dep = with_files(mul('*'), {'package.json': '{}\n'})
        self.set_scenario({'cursor': {GH: [PLAN_STEP], GX: [dep]},
                           'claude': {SONNET: [verdict('accept', WHY_ACCEPT), {'result': REVIEW_PASS}]}})
        code, doc, text = self.go(*MEDIUM)
        self.assertEqual(self.classes(doc), [('G-x', None)], text)
        self.assertEqual(code, 0, text)
        judged = self.judged()
        self.assertEqual(len(judged), 1, judged)
        self.assertIn('forbidden: package.json', judged[0]['findings'])

    def test_repair_reruns_the_same_rung_on_the_kept_tree(self):
        self.set_scenario({'cursor': {GL: [STRAYED, AGAIN]},
                           'claude': {SONNET: [verdict('repair', WHY_REPAIR, [FIX]), verdict('accept', WHY_ACCEPT),
                                               {'result': REVIEW_PASS}]}})
        code, doc, text = self.go(*EASY)
        self.assertEqual(self.classes(doc), [('G-l', 'scope_repair'), ('G-l', None)], text)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.models(), [GL, SONNET, GL, SONNET, SONNET])
        again = self.prompts(GL)[1]
        for part in ('class scope_repair', FIX, KEPT):
            self.assertIn(part, again, part)
        self.assertEqual([r['verdict'] for r in self.judged()], ['repair', 'accept'])
        self.assertEqual(self.state_of(doc['task'])['attempts'], 2)
        self.assertTrue(self.exists(STRAY))

    def test_second_repair_on_the_same_rung_is_a_reject(self):
        repair = verdict('repair', WHY_REPAIR, [FIX])
        self.set_scenario({'cursor': {GL: [STRAYED, AGAIN], GM: [{'files': README}]},
                           'claude': {SONNET: [repair, repair]}})
        code, doc, text = self.go(*EASY)
        self.assertEqual(self.classes(doc), [('G-l', 'scope_repair'), ('G-l', 'scope_rejected'), ('G-m', None)],
                         text)
        self.assertEqual(code, 0, text)
        self.assertEqual([r['verdict'] for r in self.judged()], ['repair', 'repair'])
        self.assertFalse(self.exists(STRAY))
        last = self.prompts(GM)[0]
        for part in ('class scope_rejected', RESET):
            self.assertIn(part, last, part)
        self.assertEqual(self.state_of(doc['task'])['attempts'], 3)

    def test_reject_saves_the_patch_resets_and_moves_on(self):
        self.set_scenario({'cursor': {GL: [STRAYED], GM: [{'files': README}]},
                           'claude': {SONNET: [verdict('reject', WHY_REJECT)]}})
        code, doc, text = self.go(*EASY)
        self.assertEqual(self.classes(doc), [('G-l', 'scope_rejected'), ('G-m', None)], text)
        self.assertEqual(code, 0, text)
        judged = self.judged()
        self.assertEqual([(r['verdict'], r['status']) for r in judged], [('reject', 'ok')])
        self.assertFalse(self.exists(STRAY))
        handoff = self.prompts(GM)[0]
        for part in ('class scope_rejected', WHY_REJECT, RESET):
            self.assertIn(part, handoff, part)
        with open(self.state_of(doc['task'])['rungs'][0]['patch'], encoding='utf-8') as fh:
            self.assertIn(STRAY, fh.read())
        impl = self.records('implementer')
        self.assertEqual([r['signals']['D'] for r in impl], ['flagged', 'pass'])
        self.assertEqual(impl[0].get('d_judge_ref'), judged[0]['ref'])
        self.assertEqual(self.only_task()['esc'].get('scope_rejected'), 1)
        self.assert_schema()


class TestHAndAFirst(ScopeCase):
    def test_a_failure_is_tried_wrong_without_a_judge_call(self):
        wrong = with_files(mul('+'), {HELPER: 'X = 1\n'})
        self.set_scenario({'cursor': {GH: [PLAN_STEP], GX: [wrong, {'result': REVIEW_PASS}]},
                           'claude': {SONNET: [mul('*', '  # fixed')]}})
        code, doc, text = self.go(*MEDIUM)
        self.assertEqual(self.classes(doc), [('G-x', 'tried_wrong'), ('S', None)], text)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.judged(), [])
        handoff = self.prompts(SONNET)[0]
        for part in ('out of scope: ' + HELPER, TEST_PATH + '::test_mul_basic', RESET):
            self.assertIn(part, handoff, part)
        self.assertIs(self.state_of(doc['task'])['rungs'][0]['signals']['A'], False)

    def test_h_failure_fails_on_h_without_a_judge_call(self):
        broken = {'files': {**STRAYED['files'], 'tests/test_sanity.py': FAILING}}
        self.set_scenario({'cursor': {GL: [broken], GM: [{'files': README}]}})
        code, doc, text = self.go(*EASY)
        got = self.classes(doc)
        self.assertIn(got[0][1], ('skipped', 'tried_wrong'), text)
        self.assertEqual(got[1:], [('G-m', None)], text)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.judged(), [])
        self.assertEqual(self.sonnet_calls(), [])
        handoff = self.prompts(GM)[0]
        for part in (OUT, RESET):
            self.assertIn(part, handoff, part)


class TestJudgeUnavailable(ScopeCase):
    def test_invalid_reply_twice_is_scope_rejected(self):
        self.set_scenario({'cursor': {GL: [STRAYED], GM: [{'files': README}]},
                           'claude': {SONNET: [{'result': 'not json'}, {'result': '{"verdict": "maybe"}'}]}})
        code, doc, text = self.go(*EASY)
        self.assertEqual(self.classes(doc), [('G-l', 'scope_rejected'), ('G-m', None)], text)
        self.assertEqual(code, 0, text)
        self.assertEqual([(r['status'], r['verdict']) for r in self.judged()], [('invalid', None)])
        self.assertEqual(len(self.sonnet_calls()), 2)
        self.assertIn(UNAVAILABLE, self.prompts(GM)[0])


class TestJudgeCap(ScopeCase):
    check_judge = {'scope': 'on', 'max_scope_calls': 1}

    def test_finding_past_the_cap_gets_no_call_and_is_rejected(self):
        self.set_scenario({'cursor': {GL: [STRAYED], GM: [STRAYED]},
                           'claude': {SONNET: [verdict('reject', WHY_REJECT)]}})
        code, doc, text = self.go(*EASY)
        self.assertEqual(self.classes(doc), [('G-l', 'scope_rejected'), ('G-m', 'scope_rejected')], text)
        self.assertEqual(code, 1, text)
        self.assertEqual(len(self.judged()), 1)
        self.assertEqual(len(self.sonnet_calls()), 1)
        self.assertIn(UNAVAILABLE, self.state_of(doc['task'])['reason'] or '')


if __name__ == '__main__':
    unittest.main()
