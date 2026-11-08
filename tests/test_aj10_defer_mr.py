'''AJ10: a check judge that cannot run defers the task (exit 75): state, patch, queue line, records, mr queue;
check_judge.defer off keeps the AJ8/AJ9 fallbacks.'''
import contextlib
import copy
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from mr import cli as mr_cli
from mr import config
from mr.config import ConfigError
from mr.defaults import defaults
from tests import gitfix
from tests.gitfix import git
from tests.test_loop_run import GH, GL, GM, GX, PLAN, README, REVIEW_PASS, SONNET, LoopCase, mul
from tests.test_run_once import CHECK_DEFAULT

EASY = ('--type', 'docs', '--difficulty', 'easy')
MEDIUM = ('--type', 'bugfix', '--difficulty', 'medium')
STRAY = 'sub/other.txt'
STRAYED = {'files': {STRAY: 'x\n', **README}}
OUT = 'out of scope: ' + STRAY
DENY = {'default': dict(CHECK_DEFAULT, five_hour_pct=84)}
ALLOW = {'default': dict(CHECK_DEFAULT)}
PASSING = ('import unittest\n\n\nclass TestMul(unittest.TestCase):\n'
           '    def test_mul_basic(self):\n        self.assertTrue(True)\n')
WHY_ACCEPT = 'The extra file is harmless for this task.'
WHY_REJECT = 'The plan locks a passing test; plan again.'
INVALID = [{'result': 'not json'}, {'result': '{"verdict": "maybe"}'}]


def verdict(kind, reason):
    return {'result': json.dumps({'verdict': kind, 'reason': reason, 'instructions': [], 'findings_ok': []})}


def bad_plan():
    doc = copy.deepcopy(PLAN)
    doc['tests'][0]['content'] = PASSING
    return {'result': json.dumps(doc)}


def deferred_line(check, status, task):
    return f'deferred: {check} judge unavailable ({status}); resume with: mr resume {task}'


class DeferCase(LoopCase):
    check_judge = None

    def write_cfg(self):
        super().write_cfg()
        if self.check_judge is None:
            return
        with open(self.cfgpath, encoding='utf-8') as fh:
            raw = json.load(fh)
        raw['check_judge'] = dict(self.check_judge)
        with open(self.cfgpath, 'w', encoding='utf-8') as fh:
            json.dump(raw, fh)

    def set_scenario(self, scenario, check=None):
        super().set_scenario(scenario, check)
        self.env['MR_HOME'] = self.home

    def guard(self, check):
        gitfix.write_check(self.dir, check)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = mr_cli.main(['--config', self.cfgpath, *argv])
        return code, out.getvalue(), out.getvalue() + err.getvalue()

    def go(self, *args):
        code, out, err = self.mr(*args)
        doc = json.loads(out) if out.strip() else {}
        self.assertIn('task', doc, out + err)
        return code, doc, out + err

    def defer_scope(self, check=DENY, claude=None):
        steps = claude if claude is not None else [verdict('accept', WHY_ACCEPT), {'result': REVIEW_PASS}]
        self.set_scenario({'cursor': {GL: [STRAYED], GM: [{'files': README}]}, 'claude': {SONNET: steps}},
                          check=check)
        return self.go(*EASY)

    def defer_plan(self):
        self.set_scenario({'cursor': {GH: [bad_plan()], GX: [mul('*')]},
                           'claude': {SONNET: [verdict('reject', WHY_REJECT), {'result': json.dumps(PLAN)},
                                               {'result': REVIEW_PASS}]}}, check=DENY)
        return self.go(*MEDIUM)

    def judged(self, check):
        return [r for r in self.records('check_judge') if r.get('check') == check]

    def queue_file(self):
        path = os.path.join(self.home, 'state', 'queue.jsonl')
        if not os.path.isfile(path):
            return []
        with open(path, encoding='utf-8') as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def queued(self):
        code, out, text = self.cli('--json', 'queue')
        self.assertEqual(code, 0, text)
        doc = json.loads(out)
        self.assertIsInstance(doc, list, out)
        return doc

    def exists(self, rel):
        return os.path.exists(os.path.join(self.repo, rel))

    def status(self):
        return git(self.repo, 'status', '--porcelain').stdout


class TestScopeDefer(DeferCase):
    def test_guard_denied_scope_judge_exits_75_before_the_next_rung(self):
        code, doc, text = self.defer_scope()
        self.assertNotIn(GM, self.models(), 'a judge that cannot run must not move on to the next rung')
        self.assertEqual(code, 75, text)
        self.assertEqual((doc['exit'], doc['outcome']), (75, 'deferred'), text)
        self.assertTrue(doc.get('frees_hint'), doc)
        self.assertNotIn(SONNET, self.models())
        self.assertIn(deferred_line('scope', 'guard_deny', doc['task']), text)

    def test_defer_saves_state_and_patch_and_resets_the_tree(self):
        code, doc, text = self.defer_scope()
        self.assertEqual(code, 75, text)
        st = self.state_of(doc['task'])
        self.assertEqual(st['next'], 'check_judge', st)
        pending = st.get('pending') or {}
        self.assertEqual(pending.get('check'), 'scope', pending)
        self.assertIn(OUT, pending.get('findings') or [], pending)
        self.assertEqual(st['attempts'], 1)
        with open(st['patch'], encoding='utf-8') as fh:
            self.assertIn(STRAY, fh.read())
        self.assertFalse(self.exists(STRAY), 'the tree goes back to the reset point; the patch keeps the work')
        self.assertEqual(self.status(), '')

    def test_defer_writes_one_queue_line(self):
        code, doc, text = self.defer_scope()
        self.assertEqual(code, 75, text)
        lines = self.queue_file()
        self.assertEqual(len(lines), 1, lines)
        line = lines[0]
        for key in ('task', 'repo', 'check', 'queued_at', 'frees_hint'):
            self.assertIn(key, line, key)
        self.assertEqual((line['task'], line['check']), (doc['task'], 'scope'))
        self.assertTrue(str(line['queued_at']).endswith('+05:30'), line)
        self.assertTrue(line['frees_hint'], line)

    def test_deferred_run_logs_deferred_records(self):
        code, doc, text = self.defer_scope()
        self.assertEqual(code, 75, text)
        rows = self.judged('scope')
        self.assertEqual([(r['status'], r['verdict'], r['deferred']) for r in rows], [('guard_deny', None, True)])
        task = self.only_task()
        self.assertEqual((task['exit'], task['outcome']), (75, 'deferred'))
        self.assertEqual(task.get('deferred'), {'check': 'scope', 'status': 'guard_deny'})
        self.assert_schema()

    def test_invalid_reply_twice_defers_too(self):
        code, doc, text = self.defer_scope(check=ALLOW, claude=INVALID)
        self.assertNotIn(GM, self.models())
        self.assertEqual(code, 75, text)
        self.assertEqual(self.models().count(SONNET), 2)
        self.assertIn(deferred_line('scope', 'invalid', doc['task']), text)
        self.assertEqual(self.state_of(doc['task'])['next'], 'check_judge')


class TestQueueCommand(DeferCase):
    def test_mr_queue_lists_the_waiting_task(self):
        code, doc, text = self.defer_scope()
        self.assertEqual(code, 75, text)
        entries = self.queued()
        self.assertEqual([e.get('task') for e in entries], [doc['task']], entries)
        entry = entries[0]
        self.assertEqual(entry.get('check'), 'scope')
        self.assertIn(entry.get('repo'), (self.repo, os.path.basename(self.repo)))
        self.assertTrue(entry.get('queued_at'))
        self.assertIn('frees_hint', entry)
        code, out, text = self.cli('queue')
        self.assertEqual(code, 0, text)
        self.assertIn(doc['task'], out)
        self.assertIn('scope', out)

    def test_mr_queue_is_empty_without_deferred_tasks(self):
        self.assertEqual(self.queued(), [])


class TestPlanDefer(DeferCase):
    def test_plan_judge_unavailable_defers_before_any_implementer_rung(self):
        code, doc, text = self.defer_plan()
        self.assertNotIn(GX, self.models(), 'no implementer rung starts')
        self.assertEqual(code, 75, text)
        self.assertEqual(self.models(), [GH, GH])
        st = self.state_of(doc['task'])
        self.assertEqual(st['next'], 'check_judge', st)
        pending = st.get('pending') or {}
        self.assertEqual(pending.get('check'), 'plan', pending)
        self.assertTrue(any('passed on base' in f for f in pending.get('findings') or []), pending)
        self.assertEqual((st['rungs'], st['attempts']), ([], 0))
        self.assertIn(deferred_line('plan', 'guard_deny', doc['task']), text)
        self.assertEqual([r['deferred'] for r in self.judged('plan')], [True])
        self.assertEqual(self.only_task().get('deferred'), {'check': 'plan', 'status': 'guard_deny'})
        self.assertEqual([e.get('check') for e in self.queue_file()], ['plan'])


class TestDeferOff(DeferCase):
    check_judge = {'scope': 'on', 'defer': 'off'}

    def test_defer_defaults_on_and_off_keeps_the_scope_fallback(self):
        self.assertEqual((defaults().get('check_judge') or {}).get('defer'), 'on', 'DEFAULT_YAML: check_judge.defer')
        code, doc, text = self.defer_scope()
        self.assertEqual(code, 0, text)
        rungs = [(r['rung'], r['class']) for r in self.state_of(doc['task'])['rungs']]
        self.assertEqual(rungs, [('G-l', 'scope_rejected'), ('G-m', None)], text)
        self.assertEqual([(r['status'], r['deferred']) for r in self.judged('scope')], [('guard_deny', False)])
        self.assertEqual(self.queue_file(), [])
        self.assertNotIn('resume with: mr resume', text)


class TestDeferConfig(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.home = os.path.join(os.path.realpath(td.name), 'home')
        patcher = mock.patch.dict(os.environ, {'MR_HOME': self.home, 'MR_TEST_ALLOW_FAKE_PATHS': '1'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def cfg(self, extra=None):
        raw = config._deep_merge(defaults(), {
            'paths': {'home': self.home, 'cursor_agent': gitfix.FAKE_CURSOR,
                      'claude': gitfix.FAKE_CLAUDE, 'check': gitfix.FAKE_CHECK}})
        return config.from_dict(config._deep_merge(raw, extra or {}))

    def test_defer_default_and_validation(self):
        self.assertEqual((defaults().get('check_judge') or {}).get('defer'), 'on', 'DEFAULT_YAML: check_judge.defer')
        self.assertEqual(self.cfg().check_judge.get('defer'), 'on')
        self.assertEqual(self.cfg({'check_judge': {'defer': 'off'}}).check_judge['defer'], 'off')
        for bad in ('maybe', True, 1):
            with self.assertRaises(ConfigError, msg=repr(bad)):
                self.cfg({'check_judge': {'defer': bad}})

    def test_help_lists_queue_and_resume(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = mr_cli.main(['--help'])
        self.assertEqual(code, 0)
        for cmd in ('queue', 'resume'):
            self.assertIn(cmd, out.getvalue(), cmd)


if __name__ == '__main__':
    unittest.main()
