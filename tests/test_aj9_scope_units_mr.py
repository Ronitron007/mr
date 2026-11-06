'''AJ9 units: signal D split (violations vs findings), the new failure classes, next_step and count_after for a
scope repair, the scope judge rung and prompt, and the check_judge scope keys.'''
import os
import tempfile
import unittest
from unittest import mock

from mr import checkjudge, classify, config, loop, scope
from mr.config import ConfigError
from mr.defaults import defaults
from tests import gitfix

LOCKED = 'tests/test_mul_mr.py'
ENV = '.env.local'
TASK = 'fix typo in README.md'
OUT = 'out of scope: sub/other.txt'
TOO_LARGE = 'too large: loc > 2' + chr(215) + 'est'
BASE = {'model': 'grok-4.7-high', 'rc': 0, 'is_error': False, 'timed_out': False, 'wrapper_exit_75': False,
        'hook_block': False, 'has_envelope': True, 'diff_files': ['mul.py', 'tools/helper.py'], 'diff_loc': 4,
        'mapped_files_touched': True, 'H': True, 'D': False, 'A_failing': [], 'C': None,
        'mr_result': {'status': 'done'}, 'prior_attempts': []}


def new_file(path, lines=1):
    body = ''.join(f'+line {i}\n' for i in range(lines))
    return (f'diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n'
            f'@@ -0,0 +1,{lines} @@\n{body}')


def patch_of(*files):
    return ''.join(new_file(*f) if isinstance(f, tuple) else new_file(f) for f in files).encode()


def checklist(create=(), modify=(), forbidden=(), est=50):
    return {'scope': {'modify': list(modify), 'create': list(create), 'forbidden': list(forbidden)}, 'est_loc': est}


def status(sig):
    return getattr(sig, 'status', None)


def rules(**kw):
    return classify.classify(classify.Inputs.from_dict(dict(BASE, **kw)))


class TestSignalD(unittest.TestCase):
    def test_locked_test_edit_is_a_violation(self):
        sig = scope.scope(patch_of(LOCKED, 'mul.py'), checklist=checklist(create=['mul.py', LOCKED]),
                          difficulty='medium', files_named=(), locked_files=(LOCKED,))
        self.assertEqual(status(sig), 'violation', sig)
        self.assertEqual(tuple(sig.violations), ('locked test edited: ' + LOCKED,))
        self.assertEqual(tuple(sig.findings), ())
        self.assertFalse(sig.ok)

    def test_sensitive_path_outside_the_approved_scope_is_a_violation(self):
        planned = scope.scope(patch_of(ENV, 'mul.py'), checklist=checklist(create=['mul.py']),
                              difficulty='medium', files_named=())
        self.assertEqual(status(planned), 'violation', planned)
        self.assertIn('sensitive path: ' + ENV, planned.violations)
        self.assertIn('out of scope: ' + ENV, planned.findings)
        named = scope.scope(patch_of(ENV, 'README.md'), checklist=None, difficulty='easy',
                            files_named=('README.md',))
        self.assertEqual(status(named), 'violation', named)
        self.assertIn('sensitive path: ' + ENV, named.violations)

    def test_sensitive_path_inside_the_approved_scope_passes(self):
        planned = scope.scope(patch_of(ENV, 'mul.py'), checklist=checklist(create=['mul.py', ENV]),
                              difficulty='medium', files_named=())
        self.assertEqual(status(planned), 'pass', planned)
        self.assertEqual((tuple(planned.violations), tuple(planned.findings), planned.ok), ((), (), True))
        named = scope.scope(patch_of(ENV), checklist=None, difficulty='easy', files_named=(ENV,))
        self.assertEqual(status(named), 'pass', named)

    def test_other_findings_are_flagged_for_the_judge(self):
        cases = [
            ('out of scope: tools/helper.py', patch_of('mul.py', 'tools/helper.py'), checklist(create=['mul.py'])),
            ('forbidden: notes/a.txt', patch_of('mul.py', 'notes/a.txt'),
             checklist(create=['mul.py', 'notes/a.txt'], forbidden=['notes/**'])),
            ('dependency/config file: package.json', patch_of('package.json'), checklist(create=['package.json'])),
            (TOO_LARGE, patch_of(('mul.py', 5)), checklist(create=['mul.py'], est=1)),
        ]
        for want, patch, plan in cases:
            sig = scope.scope(patch, checklist=plan, difficulty='medium', files_named=())
            self.assertEqual(status(sig), 'flagged', f'{want}: {sig}')
            self.assertIn(want, sig.findings, want)
            self.assertEqual(tuple(sig.violations), (), want)
            self.assertEqual(set(sig.findings), set(sig.reasons), want)
            self.assertFalse(sig.ok, want)


class TestClassify(unittest.TestCase):
    def test_safety_violation_comes_first_and_is_hard(self):
        got = rules(D_status='violation', H=False)
        self.assertEqual(got.cls, 'safety_violation', got)
        self.assertEqual(got.matched[0], 'safety_violation', got)
        self.assertNotIn('off_track', got.matched)
        self.assertTrue(got.hard)

    def test_scope_judge_verdicts_are_hard_classes(self):
        for verdict, want in (('repair', 'scope_repair'), ('reject', 'scope_rejected'),
                              ('unavailable', 'scope_rejected')):
            got = rules(D_status='flagged', scope_verdict=verdict)
            self.assertEqual(got.cls, want, f'{verdict}: {got}')
            self.assertTrue(got.hard, verdict)
            self.assertNotIn('off_track', got.matched, verdict)

    def test_flagged_without_a_verdict_falls_to_h_and_a(self):
        got = rules(D_status='flagged', H=False)
        self.assertEqual(got.cls, 'skipped', got)
        self.assertNotIn('off_track', got.matched)
        got = rules(D_status='flagged', A_failing=[LOCKED + '::test_mul_basic'])
        self.assertEqual(got.cls, 'tried_wrong', got)


class TestNextStep(unittest.TestCase):
    def state(self, **kw):
        base = dict(ladder=('G-l', 'G-m'), difficulty='easy', used_rungs=('G-l',), current_rung='G-l',
                    cls='scope_repair', attempts=1, claude_rungs=0, opus_rungs=0, infra_retries_this_rung=0,
                    effort_up_used=(), guard_allows=True, replans=0)
        base.update(kw)
        return loop.LoopState(**base)

    def step(self, s):
        try:
            return loop.next_step(s)
        except ValueError as exc:
            self.fail(f'next_step does not handle {s.cls}: {exc}')

    def test_scope_repair_runs_the_same_rung_on_the_kept_tree(self):
        got = self.step(self.state())
        self.assertEqual((got.action, got.rung, got.reset), ('rung', 'G-l', False), got)
        got = self.step(self.state(ladder=('S', 'O'), difficulty='medium', used_rungs=('S',), current_rung='S',
                                   claude_rungs=1))
        self.assertEqual((got.action, got.rung, got.reset), ('rung', 'S', False), got)

    def test_safety_violation_and_scope_rejected_reset_like_off_track(self):
        old = self.step(self.state(cls='off_track'))
        for cls in ('safety_violation', 'scope_rejected'):
            got = self.step(self.state(cls=cls))
            self.assertEqual((got.action, got.rung, got.reset), ('rung', 'G-m', True), cls)
            self.assertEqual((got.rung, got.reset), (old.rung, old.reset), cls)

    def test_a_repair_attempt_counts_as_an_attempt_not_a_new_rung(self):
        try:
            got = loop.count_after({'attempts': 1, 'claude_rungs': 1, 'opus_rungs': 0}, 'S', kind='scope_repair')
        except ValueError as exc:
            self.fail(f'count_after has no scope_repair kind: {exc}')
        self.assertEqual(got, {'attempts': 2, 'claude_rungs': 1, 'opus_rungs': 0})


class CfgCase(unittest.TestCase):
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


class TestScopeJudgeRung(CfgCase):
    def test_scope_rung_is_s_or_s_plus(self):
        fn = getattr(checkjudge, 'scope_rung', None)
        self.assertIsNotNone(fn, 'mr.checkjudge.scope_rung is missing')
        cfg = self.cfg()
        cases = ((('G-x', 'easy', 'low'), 'S'), (('G-h', 'medium', 'medium'), 'S'), (('K-h', 'medium', 'low'), 'S'),
                 (('S', 'easy', 'low'), 'S+'), (('S+', 'medium', 'low'), 'S+'), (('S+A', 'medium', 'low'), 'S+'),
                 (('G-x', 'hard', 'low'), 'S+'), (('G-x', 'critical', 'low'), 'S+'), (('G-x', 'easy', 'high'), 'S+'))
        for (impl, diff, risk), want in cases:
            self.assertEqual(fn(cfg, implementer=impl, difficulty=diff, risk=risk), want, (impl, diff, risk))
        strong = self.cfg({'check_judge': {'scope_rung': 'S+'}})
        self.assertEqual(fn(strong, implementer='G-x', difficulty='easy', risk='low'), 'S+')

    def test_scope_prompt_is_not_the_plan_prompt(self):
        plan = {'requirements': [], 'scope': {'modify': ['README.md'], 'create': []}, 'est_loc': 10}
        state = checkjudge.build_state('scope', TASK, plan, [OUT], signals={'H': 'pass', 'A': 'pass'}, diff='',
                                       attempt={'rung': 'G-l', 'n': 1})
        text = checkjudge.render(state)
        self.assertNotIn('mr plans again with a stronger planner', text)
        for part in ('mr.check.v1', OUT, TASK, '"verdict": "accept" | "repair" | "reject"', 'findings_ok'):
            self.assertIn(part, text, part)
        plan_text = checkjudge.render(checkjudge.build_state('plan', TASK, plan, ['an error']))
        self.assertIn('mr plans again with a stronger planner', plan_text)


class TestConfig(CfgCase):
    def test_check_judge_scope_defaults(self):
        want = {'scope': 'on', 'scope_rung': 'S', 'scope_rung_strong': 'S+', 'max_scope_calls': 3}
        block = defaults().get('check_judge') or {}
        self.assertEqual({k: block.get(k) for k in want}, want)
        loaded = self.cfg().check_judge
        self.assertEqual({k: loaded.get(k) for k in want}, want)

    def test_check_judge_scope_keys_are_validated(self):
        off = self.cfg({'check_judge': {'scope': 'off', 'max_scope_calls': 1}}).check_judge
        self.assertEqual(off['scope'], 'off')
        for key, val in (('scope', 'maybe'), ('scope', True), ('max_scope_calls', 0), ('max_scope_calls', 4),
                         ('max_scope_calls', 'three'), ('scope_rung', 'O'), ('scope_rung_strong', 'G-x'),
                         ('scope_rung', 'nope')):
            with self.assertRaises(ConfigError, msg=f'{key}={val!r}'):
                self.cfg({'check_judge': {key: val}})


if __name__ == '__main__':
    unittest.main()
