'''SHD9 locked tests: the `mr eval` command (help, usage errors, no data, zero calls and no state edits, malformed
lines, --since in IST, default labels and report paths, --json payload keys, --sweep, no task text in the report).
Synthetic log and label files in a temp MR_HOME; no network, no subprocess, no model calls.'''
import contextlib
import glob
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from mr import cli

HAIKU = 'claude-haiku-5-5'
DIFF = ('trivial', 'easy', 'medium', 'hard', 'critical')
RISK = ('low', 'medium', 'high')
FIELDS = ('type', 'difficulty', 'route', 'risk', 'risk_tags')
ARMS = ('rules', 'judge', 'classifier', 'judge+classifier')
THETAS = [0.6, 0.7, 0.75, 0.8, 0.9]
VERDICTS = ('PROMOTE', 'KEEP SHADOW', 'DEMOTE')
OPS = ('status_mix', 'failure_rate', 'latency_p50', 'latency_p95', 'calls_per_task', 'pts_est_sum',
       'usd_equiv_sum', 'cache_hit_share', 'consulted', 'sampled')
METRICS = ('coverage', 'accuracy', 'accuracy_ci', 'brier', 'ece', 'reliability', 'selective')
TS = '2026-10-20T10:00:00+05:30'
NOT_BUILT = 'not built in v1 L0'


def answers(rtype, route, diff, conf=0.95, risk=None, risk_conf=0.9):
    '''Compact judge answers as the judge record logs them (choice -> c/conf, score -> s/conf).'''
    out = {'route_class': {'c': route, 'conf': conf}, 'task_type': {'c': rtype, 'conf': conf},
           'difficulty': {'s': DIFF.index(diff), 'conf': conf}}
    if risk is not None:
        out['risk'] = {'s': RISK.index(risk), 'conf': risk_conf}
    return out


def rules(rtype='code', route='cascade', diff='medium', risk='low'):
    return {'route': route, 'type': rtype, 'difficulty': diff, 'risk': risk, 'risk_tags': []}


def clf_answers(rtype, diff='medium', risk='low', conf=0.9):
    return {'task_type': rtype, 'difficulty': diff, 'risk': risk, 'risk_tags': [], 'load_bearing': False,
            'high_judgment': False, 'scope_files_est': 1, 'ambiguity': 'low', 'novelty': 'low', 'context': 'small',
            'model': None, 'effort': None, 'needs_worktree': False, 'needs_review': False,
            'needs_clarification': False, 'confidence': conf, 'reason': 'synthetic'}


def judge_rec(tid, ref, ans, *, status='ok', consulted=True, ts=TS, latency=2000, served=HAIKU):
    ok = status in ('ok', 'ok_partial', 'cache_hit')
    return {'ts': ts, 'v': 2, 'task': tid, 'role': 'judge', 'ref': ref, 'use': 'route', 'qset': 'route.v1',
            'mode': 'shadow', 'backend': 'haiku', 'effort': None, 'model_req': HAIKU, 'model_served': served,
            'status': status, 'exit_code': 0 if ok else None, 'latency_ms': latency, 'pts_est': 0.2,
            'usd_equiv': 0.001 if ok else None, 'answers': dict(ans or {}) if ok else {}, 'decisions': {},
            'applied': [], 'overridden_by': {}, 'budget_after': {}, 'consulted': consulted}


def task_rows(tid, rules_, ans, *, status='ok', consulted=True, ts=TS, clf=None, latency=2000):
    '''Route, judge, classifier (when given) and task records for one task, shaped like `mr run` logs them.'''
    jref, cref = 'j-%s-1' % tid, 'c-%s-1' % tid
    jsum = {'mode': 'shadow', 'ref': jref, 'status': status, 'consulted': consulted, 'route': None, 'type': None,
            'difficulty': None, 'risk': None, 'risk_tags': [], 'acted_fields': [], 'unsure_fields': []}
    csum = None if clf is None else {
        'mode': 'shadow', 'ref': cref, 'status': 'ok', 'consulted': consulted, 'route': 'cascade',
        'type': clf['task_type'], 'difficulty': clf['difficulty'], 'risk': clf['risk'], 'risk_tags': [],
        'acted_fields': [], 'unsure_fields': [], 'confidence': clf['confidence']}
    rows = [{'ts': ts, 'v': 2, 'task': tid, 'role': 'route', 'fingerprint': 'fp-' + tid,
             'prepass_confident': not consulted, 'rules': rules_, 'judge': jsum, 'classifier': csum,
             'applied': dict(rules_, ladder=['G-l']), 'sources': {}, 'invariant_violation': False},
            judge_rec(tid, jref, ans, status=status, consulted=consulted, ts=ts, latency=latency)]
    if clf is not None:
        rows.append({'ts': ts, 'v': 2, 'task': tid, 'role': 'classifier', 'ref': cref, 'mode': 'shadow',
                     'model': 'grok-4.7-low', 'status': 'ok', 'consulted': consulted, 'calls': 1,
                     'latency_ms': 900, 'answers': clf, 'reason': None})
    rows.append({'ts': ts, 'v': 2, 'task': tid, 'role': 'task', 'title': 'synthetic task ' + tid,
                 'shadow': {'judge': None, 'classifier': None}})
    return rows


def small_set(n=6, ts=TS, prefix='t-a'):
    '''n consulted tasks with judge and classifier answers; the labels agree with the judge.'''
    rows, labels = [], {}
    for i in range(n):
        tid = '%s%03d' % (prefix, i)
        rtype = ('code', 'bugfix')[i % 2]
        rows += task_rows(tid, rules(), answers(rtype, 'cascade', 'medium'), ts=ts, clf=clf_answers(rtype))
        labels[tid] = {'type': rtype, 'route': 'cascade', 'difficulty': 'medium', 'risk': 'low'}
    return rows, labels


def snapshot(root):
    out = {}
    for base, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(base, name)
            with open(path, 'rb') as fh:
                out[os.path.relpath(path, root)] = fh.read()
    return out


class EvalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mr-shd9-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(os.path.join(self.home, 'log'))
        self.out = os.path.join(self.tmp, 'report.md')
        env = mock.patch.dict(os.environ, {'MR_HOME': self.home, 'MR_SKILLSYNC': '0',
                                           'XDG_CONFIG_HOME': os.path.join(self.tmp, 'cfg')})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('MR_CONFIG', None)

    def write_log(self, rows, garbage=()):
        with open(os.path.join(self.home, 'log', 'runs.jsonl'), 'w', encoding='utf-8') as fh:
            for row in rows:
                fh.write(json.dumps(row) + '\n')
            for line in garbage:
                fh.write(line + '\n')

    def write_labels(self, labels, path=None):
        path = path or os.path.join(self.home, 'eval', 'labels.jsonl')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            for tid, lab in labels.items():
                fh.write(json.dumps({'task': tid, 'labels': lab}) + '\n')
        return path

    def mr(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def payload(self, *extra):
        code, out, err = self.mr('--json', 'eval', '--out', self.out, *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def report(self):
        with open(self.out, encoding='utf-8') as fh:
            return fh.read()


class TestEvalCli(EvalCase):
    def test_eval_help_exits_zero(self):
        code, out, err = self.mr('eval', '--help')
        self.assertEqual(code, 0, err)
        self.assertNotIn(NOT_BUILT, err)
        for flag in ('--since', '--labels', '--out', '--sweep'):
            self.assertIn(flag, out, flag)

    def test_eval_usage_errors_exit_2(self):
        code, _out, err = self.mr('eval', 'extra', 'args')
        self.assertEqual(code, 2, err)
        self.assertNotIn(NOT_BUILT, err)
        code, _out, err = self.mr('eval', '--since', '31-10-2026', '--out', self.out)
        self.assertEqual(code, 2, err)
        self.assertNotIn(NOT_BUILT, err)
        self.assertIn('--since', err)
        self.assertFalse(os.path.exists(self.out))

    def test_no_data_report(self):
        code, _out, err = self.mr('eval', '--out', self.out)
        self.assertEqual(code, 0, err)
        self.assertIn('no shadow data', self.report())
        p = self.payload()
        self.assertTrue(p['no_data'])
        self.assertEqual(p['tasks'], 0)
        for name in ('judge', 'classifier'):
            dec = p['deciders'][name]
            for f in FIELDS:
                self.assertEqual(dec['verdicts'][f], 'KEEP SHADOW', (name, f))
                self.assertTrue(dec['missing'][f], (name, f))

    def test_zero_calls_and_no_state_edits(self):
        rows, labels = small_set()
        self.write_log(rows)
        self.write_labels(labels)
        before = snapshot(self.home)
        boom = mock.Mock(side_effect=AssertionError('mr eval must not spawn a process or open a socket'))
        with mock.patch('subprocess.Popen', boom), mock.patch('subprocess.run', boom):
            with mock.patch('socket.socket', boom):
                code, _out, err = self.mr('eval', '--out', self.out)
        self.assertEqual(code, 0, err)
        self.assertEqual(boom.call_count, 0)
        self.assertTrue(os.path.isfile(self.out))
        self.assertEqual(snapshot(self.home), before)

    def test_malformed_lines_skipped(self):
        rows, _labels = small_set(3)
        self.write_log(rows, garbage=('not json', '[1, 2]', '{"role": "route", "task"'))
        p = self.payload()
        self.assertEqual(p['tasks'], 3)
        self.assertFalse(p['no_data'])

    def test_since_filters_by_ist_date(self):
        old, _ = small_set(2, ts='2026-10-01T10:00:00+05:30', prefix='t-old')
        new, _ = small_set(3, prefix='t-new')
        edge, _ = small_set(1, ts='2026-10-14T20:00:00+00:00', prefix='t-edge')  # 15 Oct 01:30 IST
        self.write_log(old + new + edge)
        self.assertEqual(self.payload()['tasks'], 6)
        p = self.payload('--since', '2026-10-15')
        self.assertEqual(p['tasks'], 4)
        self.assertEqual(p['since'], '2026-10-15')

    def test_default_labels_and_report_path(self):
        rows, labels = small_set(6)
        self.write_log(rows)
        known = sorted(labels)
        path = os.path.join(self.home, 'eval', 'labels.jsonl')
        os.makedirs(os.path.dirname(path))
        with open(path, 'w', encoding='utf-8') as fh:
            for tid in known[:4]:
                fh.write(json.dumps({'task': tid, 'labels': labels[tid]}) + '\n')
            fh.write('not json\n')
            fh.write(json.dumps({'task': 't-unknown', 'labels': {'type': 'code'}}) + '\n')
        code, out, err = self.mr('--json', 'eval')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['labelled'], 4)
        reports = glob.glob(os.path.join(self.home, 'eval', 'reports', '????-??-??.md'))
        self.assertEqual(len(reports), 1, reports)
        other = self.write_labels({tid: labels[tid] for tid in known[:2]}, os.path.join(self.tmp, 'other.jsonl'))
        self.assertEqual(self.payload('--labels', other)['labelled'], 2)

    def test_json_payload_keys(self):
        rows, labels = small_set(6)
        self.write_log(rows)
        self.write_labels(labels)
        code, out, err = self.mr('--json', 'eval', '--out', self.out)
        self.assertEqual(code, 0, err)
        p = json.loads(out)
        for key in ('no_data', 'since', 'tasks', 'labelled', 'arms', 'deciders'):
            self.assertIn(key, p)
        self.assertEqual((p['tasks'], p['labelled']), (6, 6))
        for arm in ARMS:
            for f in FIELDS:
                for key in ('n', 'coverage', 'agreement', 'accuracy'):
                    self.assertIn(key, p['arms'][arm][f], (arm, f, key))
        for name in ('judge', 'classifier'):
            dec = p['deciders'][name]
            for key in ('verdicts', 'missing', 'fields', 'ops', 'under_routes'):
                self.assertIn(key, dec, (name, key))
            for f in FIELDS:
                self.assertIn(dec['verdicts'][f], VERDICTS, (name, f))
                self.assertIsInstance(dec['missing'][f], list)
                m = dec['fields'][f]
                for key in METRICS:
                    self.assertIn(key, m, (name, f, key))
                self.assertEqual(len(m['reliability']), 5, (name, f))
                self.assertEqual([round(e['theta'], 2) for e in m['selective']], THETAS, (name, f))
            for f in ('type', 'route'):
                self.assertIn('macro_f1', dec['fields'][f], (name, f))
            for f in ('difficulty', 'risk'):
                self.assertIn('mae', dec['fields'][f], (name, f))
            for key in OPS:
                self.assertIn(key, dec['ops'], (name, key))
        self.assertTrue(os.path.isfile(self.out))

    def test_sweep_prints_thresholds(self):
        rows, labels = small_set(4)
        self.write_log(rows)
        self.write_labels(labels)
        code, out, err = self.mr('eval', '--sweep', '--out', self.out)
        self.assertEqual(code, 0, err)
        self.assertIn('theta', out.lower())
        for theta in ('0.6', '0.7', '0.75', '0.8', '0.9'):
            self.assertIn(theta, out, theta)

    def test_report_has_sections_and_no_task_text(self):
        rows, labels = small_set(3)
        canary = 'CANARY-' + 'shd9-task-text'
        rows[0]['task_text'] = 'Fix the thing ' + canary
        rows[1]['state'] = {'task': canary}
        self.write_log(rows)
        self.write_labels(labels)
        code, out, err = self.mr('--json', 'eval', '--out', self.out)
        self.assertEqual(code, 0, err)
        self.assertNotIn(canary, out)
        report = self.report()
        self.assertNotIn(canary, report)
        low = report.lower()
        self.assertIn('judge', low)
        self.assertIn('classifier', low)
        self.assertTrue(any(v in report for v in VERDICTS), report)


if __name__ == '__main__':
    unittest.main()
