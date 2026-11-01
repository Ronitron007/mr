'''SHD9 locked tests: `mr eval` verdicts and metrics (AC-EVAL-1: a perfect judge promotes, a random judge does not,
a high-risk under-route blocks promotion; model drift demotes; offline re-threshold; judge+classifier arm; ops;
reproducible bootstrap CI). Synthetic logs in a temp MR_HOME, no calls.'''
import unittest

from tests.test_shd9_eval_cli_mr import FIELDS, EvalCase, answers, clf_answers, judge_rec, rules, task_rows


def labelled_set(n=60, wrong=False, under=False):
    '''n consulted labelled tasks. The rules (code, cascade) get type and route right on even tasks only; every third
    task is medium. The judge answers the label (wrong=True: the other value) with confidence 0.95.
    under=True: task 0 is labelled risk high while the rules and the judge say low.'''
    rows, labels = [], {}
    for i in range(n):
        tid = 't-%04d' % i
        good = ('code', 'cascade') if i % 2 == 0 else ('bugfix', 'judgment')
        bad = ('bugfix', 'judgment') if i % 2 == 0 else ('code', 'cascade')
        diff = 'medium' if i % 3 == 0 else 'easy'
        rtype, route = bad if wrong else good
        risk = 'low' if under and i == 0 else None
        rows += task_rows(tid, rules(diff=diff), answers(rtype, route, diff, risk=risk))
        labels[tid] = {'type': good[0], 'route': good[1], 'difficulty': diff}
        if under and i == 0:
            labels[tid]['risk'] = 'high'
    return rows, labels


class TestEvalVerdicts(EvalCase):
    def judge(self, rows, labels=None):
        self.write_log(rows)
        if labels:
            self.write_labels(labels)
        return self.payload()['deciders']['judge']

    def test_perfect_judge_promotes(self):
        rows, labels = labelled_set()
        self.write_log(rows)
        self.write_labels(labels)
        p = self.payload()
        judge = p['deciders']['judge']
        for f in ('type', 'route'):
            self.assertEqual(judge['verdicts'][f], 'PROMOTE', (f, judge['missing'][f]))
        self.assertEqual(judge['under_routes'], 0)
        self.assertAlmostEqual(judge['fields']['type']['brier'], 0.0025, places=4)
        self.assertEqual(p['labelled'], 60)
        self.assertAlmostEqual(p['arms']['rules']['type']['accuracy'], 0.5)
        self.assertAlmostEqual(p['arms']['judge']['type']['accuracy'], 1.0)
        self.assertAlmostEqual(p['arms']['judge']['type']['agreement'], 0.5)
        self.assertIn('PROMOTE', self.report())

    def test_random_judge_never_promotes(self):
        judge = self.judge(*labelled_set(wrong=True))
        for f in ('type', 'route'):
            self.assertIn(judge['verdicts'][f], ('DEMOTE', 'KEEP SHADOW'), f)

    def test_high_risk_under_route_blocks_promote(self):
        judge = self.judge(*labelled_set(under=True))
        self.assertEqual(judge['under_routes'], 1)
        for f in FIELDS:
            self.assertNotEqual(judge['verdicts'][f], 'PROMOTE', f)
        self.assertEqual(judge['verdicts']['type'], 'DEMOTE')

    def test_model_drift_demotes(self):
        rows, labels = labelled_set()
        rows.append(judge_rec('t-0001', 'j-t-0001-2', None, status='model_drift', served='claude-other-model'))
        judge = self.judge(rows, labels)
        for f in ('type', 'route'):
            self.assertEqual(judge['verdicts'][f], 'DEMOTE', f)

    def test_offline_rethreshold_changes_coverage(self):
        rows, labels = [], {}
        for i in range(20):
            tid = 't-r%03d' % i
            conf = 0.65 if i % 2 == 0 else 0.95
            rows += task_rows(tid, rules(), answers('bugfix', 'cascade', 'medium', conf=conf))
            labels[tid] = {'type': 'bugfix'}
        judge = self.judge(rows, labels)
        sel = {round(e['theta'], 2): e for e in judge['fields']['type']['selective']}
        self.assertAlmostEqual(sel[0.6]['coverage'], 1.0)
        self.assertAlmostEqual(sel[0.9]['coverage'], 0.5)
        self.assertAlmostEqual(sel[0.9]['accuracy'], 1.0)
        self.assertAlmostEqual(judge['fields']['type']['coverage'], 0.5)

    def test_judge_plus_classifier_fills_unsure_medium_plus(self):
        rows = []
        for i in range(20):
            diff = 'medium' if i < 10 else 'easy'
            rows += task_rows('t-c%03d' % i, rules(diff=diff), answers('bugfix', 'cascade', diff, conf=0.6),
                              clf=clf_answers('bugfix', diff=diff))
        self.write_log(rows)
        arms = self.payload()['arms']
        self.assertAlmostEqual(arms['judge']['type']['coverage'], 0.0)
        self.assertAlmostEqual(arms['classifier']['type']['coverage'], 1.0)
        self.assertAlmostEqual(arms['judge+classifier']['type']['coverage'], 0.5)

    def test_ops_failure_rate_and_status_mix(self):
        rows = []
        for i, status in enumerate(['ok'] * 8 + ['timeout', 'cli_error']):
            rows += task_rows('t-o%03d' % i, rules(), answers('code', 'cascade', 'medium'), status=status,
                              consulted=i < 6, latency=1000 * (i + 1))
        ops = self.judge(rows)['ops']
        self.assertAlmostEqual(ops['failure_rate'], 0.2)
        self.assertEqual(ops['status_mix'], {'ok': 8, 'timeout': 1, 'cli_error': 1})
        self.assertEqual((ops['consulted'], ops['sampled']), (6, 4))
        self.assertAlmostEqual(ops['pts_est_sum'], 2.0)
        self.assertAlmostEqual(ops['usd_equiv_sum'], 0.008)
        self.assertAlmostEqual(ops['calls_per_task'], 1.0)
        self.assertAlmostEqual(ops['cache_hit_share'], 0.0)
        self.assertGreaterEqual(ops['latency_p95'], ops['latency_p50'])

    def test_bootstrap_ci_reproducible(self):
        rows, labels = [], {}
        for i in range(20):
            tid = 't-b%03d' % i
            said = 'bugfix' if i < 15 else 'refactor'
            rows += task_rows(tid, rules(), answers(said, 'cascade', 'medium'))
            labels[tid] = {'type': 'bugfix'}
        self.write_log(rows)
        self.write_labels(labels)
        first = self.payload()['deciders']['judge']['fields']['type']
        second = self.payload()['deciders']['judge']['fields']['type']
        self.assertAlmostEqual(first['accuracy'], 0.75)
        self.assertEqual(first['accuracy_ci'], second['accuracy_ci'])
        lo, hi = first['accuracy_ci']
        self.assertLess(lo, hi)
        self.assertLessEqual(lo, 0.75)
        self.assertGreaterEqual(hi, 0.75)


if __name__ == '__main__':
    unittest.main()
