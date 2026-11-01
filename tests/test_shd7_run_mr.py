'''SHD7 locked tests: `--judge shadow` in `mr run` (gate, pending route summary, join at task end, task summary,
AC-JDG-1 and AC-JDG-2 full runs, a crash never changes the exit) and in `mr route` (line, JSON key, --no-models).
Fake claude and fake cursor-agent only.'''
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from mr import cli, shadow
from mr.judge import questions as qmod
from mr.task import Params, Task, fingerprint
from tests import gitfix
from tests.gitfix import git
from tests.test_run_once import GL, RunCase

HAIKU = 'claude-haiku-5-5'
UNSURE = 'maybe fix typo in README.md or something?'
README = {'files': {'README.md': 'fixed typo'}}
GIVEN = ('--type', 'docs', '--difficulty', 'trivial', '--route', 'cascade')
ON_LINE = 'judge on not built; running as shadow'
LINE = 'judge (shadow): '
TEXTS = (UNSURE, 'maybe update the helper in src/util.py?', 'document the typo in README.md please')
VOLATILE = {'ts', 'task', 'started_at', 'ended_at', 'wall_s', 'ttg_s', 'raw_dir', 'cost'}
ROUTE_KEYS = ('fingerprint', 'prepass_confident', 'rules', 'applied', 'sources', 'invariant_violation')
TASK_KEYS = ('type', 'difficulty', 'risk', 'risk_tags', 'route', 'route_src', 'ladder', 'first_rung', 'final_rung',
             'esc', 'outcome', 'exit', 'final_signals', 'files_n')


def answers_json():
    '''Adversarial judge: route design, type docs, every score at its top, every yes/no yes, confidence 1.0.'''
    out = {}
    for i, (qid, q) in enumerate(qmod.load('route.v1').items(), 1):
        if qid == 'route_class':
            ans = 'design'
        elif qid == 'task_type':
            ans = 'docs'
        elif q.get('type') == 'choice':
            ans = list(q['criteria'])[-1]
        elif q.get('type') == 'score':
            ans = len(q['criteria']) - 1
        else:
            ans = True
        out['q%02d' % i] = {'answer': ans, 'confidence': 1.0}
    return json.dumps({'answers': out})


def _strip(rec):
    return {k: v for k, v in rec.items() if k not in VOLATILE}


def _decision(recs):
    route = [r for r in recs if r['role'] == 'route']
    task = [r for r in recs if r['role'] == 'task']
    impl = [(r['rung'], r.get('failure_class')) for r in recs if r['role'] == 'implementer']
    return ({k: route[0].get(k) for k in ROUTE_KEYS} if route else None,
            {k: task[0].get(k) for k in TASK_KEYS} if task else None, impl)


def _plain(out):
    return [ln for ln in out.splitlines() if not ln.startswith(('task_id:', LINE))]


class JudgeRunCase(RunCase):
    judge = None

    def setUp(self):
        super().setUp()
        self.set_scenario({'cursor': {GL: [README]}, 'claude': {HAIKU: [{'result': answers_json()}]}})

    def set_scenario(self, scenario, check=None):
        super().set_scenario(scenario, check)
        self.env['MR_HOME'] = self.home

    def write_cfg(self):
        block = {'timeouts_ms': {'call': 600000, 'decision_point': 300000}}
        block.update(self.judge or {})
        raw = {'paths': {'home': self.home, 'cursor_agent': gitfix.FAKE_CURSOR, 'claude': gitfix.FAKE_CLAUDE,
                         'check': gitfix.FAKE_CHECK}, 'judge': block}
        with open(self.cfgpath, 'w', encoding='utf-8') as fh:
            json.dump(raw, fh)

    def set_judge(self, **kw):
        self.judge = kw
        self.write_cfg()

    def judge_calls(self):
        return [c for c in self.calls() if c.get('cli') == 'claude' and c.get('model') == HAIKU]

    def reset_repo(self):
        git(self.repo, 'checkout', '-q', '-f', self.branch)
        git(self.repo, 'reset', '-q', '--hard')
        git(self.repo, 'clean', '-q', '-f', '-d')

    def find_text(self, sampled):
        '''A task text whose fingerprint is (or is not) in the default 1-in-3 sample.'''
        base = gitfix.head(self.repo)
        params = Params(type='docs', difficulty='trivial', route='cascade')
        for i in range(500):
            text = f'fix typo in README.md, case {i}'
            task = Task(id='t-00000000', text=text, repo=self.repo, base=base, params=params,
                        files_named=('README.md',), source='cli')
            if shadow.should_run(params, True, fingerprint(task), 3)[0] == sampled:
                return text
        self.fail('no fingerprint found')

    def route(self, *extra, text=UNSURE, json_out=False):
        argv = (['--config', self.cfgpath] + (['--json'] if json_out else [])
                + ['route', text, '--repo', self.repo, *extra])
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


class JudgeRunTest(JudgeRunCase):
    def test_off_and_default_unchanged_and_shadow_adds_only_judge(self):
        snaps, decisions = [], []
        for extra in ((), ('--judge', 'off'), ('--judge', 'shadow')):
            start = len(self.log())
            code, _, err = self.mr(*extra, text=UNSURE)
            recs = self.log()[start:]
            self.reset_repo()
            route = [r for r in recs if r['role'] == 'route']
            task = [r for r in recs if r['role'] == 'task']
            judged = [r for r in recs if r['role'] == 'judge']
            self.assertEqual((len(route), len(task)), (1, 1), err)
            decisions.append((code, _decision(recs)))
            if extra == ('--judge', 'shadow'):
                self.assertEqual([r['status'] for r in judged], ['ok'], err)
                self.assertEqual(len(self.judge_calls()), 1)
                continue
            self.assertEqual(judged, [], extra)
            self.assertIsNone(route[0]['judge'], extra)
            self.assertIsNone(task[0]['shadow']['judge'], extra)
            self.assertEqual(self.judge_calls(), [], extra)
            snaps.append((code, _strip(route[0]), _strip(task[0])))
        self.assertEqual(snaps[0], snaps[1])
        self.assertEqual(decisions[0], decisions[2])
        self.assertEqual(decisions[1], decisions[2])

    def test_unsure_shadow_run_logs_ok_judge_and_pending_ref(self):
        code, _, err = self.mr('--judge', 'shadow', text=UNSURE)
        task = self.only_task()
        ref = 'j-' + task['task'] + '-1'
        recs = self.records('judge')
        self.assertEqual(len(recs), 1, err)
        r = recs[0]
        got = (r['ref'], r['status'], r['mode'], r['consulted'], r['applied'], r['use'], r['model_req'])
        self.assertEqual(got, (ref, 'ok', 'shadow', True, [], 'route', HAIKU))
        self.assertTrue(r['answers'])
        self.assertTrue(r['decisions'])
        self.assertNotIn('prompt', r)
        routes = self.records('route')
        self.assertEqual(len(routes), 1)
        summ = routes[0]['judge'] or {}
        self.assertEqual((summ.get('status'), summ.get('ref'), summ.get('consulted')), ('pending', ref, True))
        self.assertEqual(routes[0]['applied']['route'], routes[0]['rules']['route'])
        self.assertEqual(set(task['shadow']['judge'] or {}), {'route', 'first_rung'})
        self.assertEqual(task['shadow']['judge']['route'], 'design')
        self.assertEqual(len(self.judge_calls()), 1)
        self.assert_schema()

    def test_confident_unsampled_task_is_skipped(self):
        text = self.find_text(False)
        code, _, err = self.mr('--judge', 'shadow', *GIVEN, text=text)
        recs = self.records('judge')
        self.assertEqual([(r['status'], r['consulted']) for r in recs], [('skipped_confident', False)], err)
        self.assertEqual(self.judge_calls(), [])
        routes = self.records('route')
        self.assertEqual(len(routes), 1)
        summ = routes[0]['judge'] or {}
        self.assertEqual((summ.get('status'), summ.get('consulted')), ('skipped_confident', False))
        self.assertIsNone(self.only_task()['shadow']['judge'])
        self.assert_schema()

    def test_confident_sampled_task_runs_unconsulted(self):
        text = self.find_text(True)
        code, _, err = self.mr('--judge', 'shadow', *GIVEN, text=text)
        recs = self.records('judge')
        self.assertEqual([(r['status'], r['consulted']) for r in recs], [('ok', False)], err)
        self.assertEqual(len(self.judge_calls()), 1)
        summ = self.records('route')[0]['judge'] or {}
        self.assertEqual((summ.get('status'), summ.get('consulted')), ('pending', False))
        self.assertEqual((self.only_task()['shadow']['judge'] or {}).get('route'), 'design')
        self.assert_schema()

    def test_adversarial_judge_full_runs_unchanged(self):
        self.set_judge(sample_n=1)
        for text in TEXTS:
            got = {}
            for mode in ('off', 'shadow'):
                start = len(self.log())
                code, _, err = self.mr('--judge', mode, text=text)
                recs = self.log()[start:]
                self.reset_repo()
                got[mode] = (code, _decision(recs))
                judged = [r for r in recs if r['role'] == 'judge']
                if mode == 'off':
                    self.assertEqual(judged, [], text)
                    continue
                self.assertEqual([(r['status'], r['applied']) for r in judged], [('ok', [])], (text, err))
                route = [r for r in recs if r['role'] == 'route']
                self.assertEqual(len(route), 1, (text, err))
                applied = {k: v for k, v in route[0]['applied'].items() if k != 'ladder'}
                self.assertEqual(applied, route[0]['rules'], text)
                self.assertEqual((route[0]['judge'] or {}).get('acted_fields'), [], text)
            self.assertEqual(got['off'], got['shadow'], text)
        self.assertEqual(len(self.judge_calls()), 3)

    def test_judge_crash_is_logged_and_never_changes_exit(self):
        self.set_judge(sample_n=1)
        with patch('mr.guard.run_claude_judge', side_effect=RuntimeError('judge boom')):
            code, _, err = self.mr('--judge', 'shadow')
        self.assertEqual(code, 0, err)
        recs = self.records('judge')
        self.assertEqual([r['status'] for r in recs], ['cli_error'], err)
        self.assertIsNone(self.only_task()['shadow']['judge'])
        self.assertEqual(self.judge_calls(), [])

    def test_run_judge_on_runs_as_shadow(self):
        code, _, err = self.mr('--judge', 'on', text=UNSURE)
        self.assertEqual(err.count(ON_LINE), 1, err)
        self.assertNotIn('running as off', err)
        recs = self.records('judge')
        self.assertEqual([(r['mode'], r['status']) for r in recs], [('shadow', 'ok')], err)


class JudgeRouteTest(JudgeRunCase):
    def test_route_shadow_prints_judge_line_and_json_key(self):
        off_code, off_out, _ = self.route('--judge', 'off')
        code, out, err = self.route('--judge', 'shadow')
        self.assertNotIn('judge not built', err)
        lines = [ln for ln in out.splitlines() if ln.startswith(LINE)]
        self.assertEqual(len(lines), 1, out + err)
        self.assertTrue(lines[0].startswith(LINE + 'route=design type=docs difficulty=critical risk=high'), lines[0])
        self.assertIn('status=ok', lines[0])
        self.assertEqual(code, off_code)
        self.assertEqual(_plain(out), _plain(off_out))
        code, out, err = self.route('--judge', 'shadow', text='maybe fix wording in README.md?', json_out=True)
        js = json.loads(out).get('judge_shadow') or {}
        self.assertEqual((js.get('status'), js.get('route')), ('ok', 'design'), out + err)
        self.assertTrue(str(js.get('ref')).startswith('j-'), js)
        self.assertEqual(len(self.judge_calls()), 2)
        self.assertEqual(self.log(), [])

    def test_route_no_models_makes_no_judge_call(self):
        code, out, err = self.route('--judge', 'shadow', '--no-models')
        self.assertNotIn('judge not built', err)
        self.assertNotIn(LINE, out)
        code, out, err = self.route('--judge', 'shadow', '--no-models', json_out=True)
        self.assertNotIn('judge not built', err)
        self.assertNotIn('judge_shadow', json.loads(out))
        self.assertEqual(self.judge_calls(), [])

    def test_route_cli_shadow_runs_for_confident_task(self):
        text = self.find_text(False)
        code, out, err = self.route('--judge', 'shadow', *GIVEN, text=text)
        lines = [ln for ln in out.splitlines() if ln.startswith(LINE)]
        self.assertEqual(len(lines), 1, out + err)
        self.assertEqual(len(self.judge_calls()), 1)
        self.assertEqual(self.log(), [])


if __name__ == '__main__':
    unittest.main()
