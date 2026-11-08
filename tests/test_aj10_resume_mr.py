'''AJ10: mr resume runs the deferred judge step again with the saved state and ends the task normally;
it refuses a task that is not deferred (exit 2) and a repo whose HEAD moved (exit 6).'''
import json
import unittest

from tests import gitfix
from tests.test_aj10_defer_mr import ALLOW, EASY, STRAY, DeferCase, deferred_line
from tests.test_loop_run import GH, GL, GM, GX, README, SONNET


class ResumeCase(DeferCase):
    def resume(self, task):
        code, out, text = self.cli('--json', 'resume', task)
        doc = json.loads(out) if out.strip().startswith('{') else {}
        return code, doc, text

    def deferred_scope_task(self):
        code, doc, text = self.defer_scope()
        self.assertEqual(code, 75, text)
        return doc['task']


class TestResumeScope(ResumeCase):
    def test_resume_with_the_judge_available_accepts_after_review(self):
        tid = self.deferred_scope_task()
        self.guard(ALLOW)
        code, doc, text = self.resume(tid)
        self.assertEqual(code, 0, text)
        self.assertEqual((doc.get('task'), doc.get('exit'), doc.get('outcome')), (tid, 0, 'accepted'), text)
        self.assertEqual(self.models().count(GL), 1, 'the deferred attempt continues; no new implementer run')
        self.assertNotIn(GM, self.models())
        rows = self.judged('scope')
        self.assertEqual([(r['verdict'], r['deferred']) for r in rows], [(None, True), ('accept', False)], rows)
        self.assertEqual(len(self.records('reviewer')), 1, 'a judge-accepted attempt gets review C')
        self.assertTrue(self.exists(STRAY), 'the tree is restored from the saved patch')
        st = self.state_of(tid)
        self.assertEqual((st['next'], st['outcome'], st['attempts']), ('done', 'accepted', 1))
        self.assertEqual((st['rungs'][-1]['rung'], st['rungs'][-1]['class']), ('G-l', None))

    def test_resume_logs_a_resumed_task_record_and_clears_the_queue(self):
        tid = self.deferred_scope_task()
        self.guard(ALLOW)
        code, doc, text = self.resume(tid)
        self.assertEqual(code, 0, text)
        tasks = self.records('task')
        self.assertEqual([t['outcome'] for t in tasks], ['deferred', 'accepted'], tasks)
        last = tasks[-1]
        self.assertEqual((last['task'], last['exit'], last.get('resumed')), (tid, 0, True))
        self.assertIsInstance(last.get('resumed_from_ts'), str)
        self.assertTrue(last['resumed_from_ts'])
        self.assertEqual(self.queued(), [])
        self.assert_schema()


class TestResumePlan(ResumeCase):
    def test_resume_after_plan_defer_continues_planning(self):
        code, doc, text = self.defer_plan()
        self.assertEqual(code, 75, text)
        tid = doc['task']
        self.guard(ALLOW)
        code, doc, text = self.resume(tid)
        self.assertEqual(code, 0, text)
        self.assertEqual(self.models().count(GH), 2, 'the first planner does not run again')
        self.assertIn(GX, self.models())
        rows = self.judged('plan')
        self.assertEqual([(r['verdict'], r['deferred']) for r in rows], [(None, True), ('reject', False)], rows)
        self.assertIn('passed on base', self.prompts(SONNET)[1], 'the stronger planner gets the plan errors')
        st = self.state_of(tid)
        self.assertEqual((st['next'], st['outcome']), ('done', 'accepted'))
        self.assertEqual(st['checklist']['goal'], 'Add mul.')
        self.assertIs(self.records('task')[-1].get('resumed'), True)


class TestResumeRefuses(ResumeCase):
    def test_resume_after_head_moved_exits_6_and_changes_nothing(self):
        tid = self.deferred_scope_task()
        gitfix.commit(self.repo, {'other.txt': 'x\n'}, 'move head')
        self.guard(ALLOW)
        before = self.state_of(tid)
        code, doc, text = self.resume(tid)
        self.assertEqual(code, 6, text)
        self.assertIn('HEAD moved', text)
        self.assertEqual(self.state_of(tid), before)
        self.assertFalse(self.exists(STRAY))
        self.assertNotIn(SONNET, self.models())
        self.assertEqual([e.get('task') for e in self.queued()], [tid])

    def test_resume_of_a_task_that_is_not_deferred_exits_2(self):
        self.set_scenario({'cursor': {GL: [{'files': README}]}})
        code, doc, text = self.go(*EASY)
        self.assertEqual(code, 0, text)
        tid = doc['task']
        code, _, text = self.resume(tid)
        self.assertEqual(code, 2, text)
        self.assertIn(f'{tid} is not deferred', text)
        code, _, text = self.resume('t-nope')
        self.assertEqual(code, 2, text)
        self.assertIn('t-nope is not deferred', text)
        self.assertEqual(self.state_of(tid)['next'], 'done')

    def test_defer_twice_keeps_one_queue_entry(self):
        tid = self.deferred_scope_task()
        code, doc, text = self.resume(tid)
        self.assertEqual(code, 75, text)
        self.assertIn(deferred_line('scope', 'guard_deny', tid), text)
        self.assertEqual([e.get('task') for e in self.queued()], [tid])
        self.assertEqual([line['task'] for line in self.queue_file()], [tid], 'the entry is updated, not duplicated')
        st = self.state_of(tid)
        self.assertEqual((st['next'], st['attempts']), ('check_judge', 1))
        self.assertFalse(self.exists(STRAY))
        self.assertEqual(self.status(), '')
        self.assertEqual(self.models().count(GL), 1)


if __name__ == '__main__':
    unittest.main()
