'''SHD3 locked tests: `mr route` classifier shadow (line, JSON key, gate), effective mode, classifier config keys.'''
import contextlib
import copy
import io
import json
import os
import threading
import unittest
from unittest.mock import patch

from mr import classifier as clf_mod
from mr import cli
from mr.config import ConfigError, from_dict
from mr.defaults import defaults
from tests import gitfix
from tests.test_run_once import RunCase

UNSURE = 'maybe fix typo in README.md or something?'
CONFIDENT = 'fix typo in README.md'
GIVEN = ('--type', 'docs', '--difficulty', 'trivial', '--route', 'cascade')
NOT_BUILT = 'judge/classifier not built'
ON_LINE = 'classifier on not built; running as shadow'
LINE = 'classifier (shadow):'


def _out(task_type):
    return clf_mod.ClassifierOut(
        task_type=task_type, difficulty='critical', risk='high', risk_tags=('payments',), load_bearing=True,
        high_judgment=True, scope_files_est=40, ambiguity='high', novelty='novel', context='large', model='O',
        effort='xhigh', needs_worktree=True, needs_review=True, needs_clarification=True, confidence=1.0,
        reason='adversarial fake')


class FakeClassifier:
    '''Stands in for mr.classifier.classify and counts its calls.'''

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, task, features, cfg, *args, **kw):
        ref = kw.get('ref', args[0] if args else None)
        with self.lock:
            self.calls.append((task.id, ref))
        other = 'ios-native' if features.type != 'ios-native' else 'docs'
        return clf_mod.ClassifyResult(out=_out(other), status='ok', calls=1, latency_ms=5,
                                      prompt_sha='0' * 12, ref=ref, redactions={}, model='grok-4.7-low')


def _lines(out):
    return [ln for ln in out.splitlines() if ln.startswith(LINE)]


def _decision_lines(out):
    return [ln for ln in out.splitlines() if not ln.startswith(('task_id:', LINE))]


class RouteShadowTest(RunCase):
    clf = None

    def set_scenario(self, scenario, check=None):
        super().set_scenario(scenario, check)
        self.env['MR_HOME'] = self.home

    def write_cfg(self):
        raw = {'paths': {'home': self.home, 'cursor_agent': gitfix.FAKE_CURSOR,
                         'claude': gitfix.FAKE_CLAUDE, 'check': gitfix.FAKE_CHECK},
               'judge': {'mode': 'off'}}
        if self.clf:
            raw['classifier'] = dict(self.clf)
        with open(self.cfgpath, 'w', encoding='utf-8') as fh:
            json.dump(raw, fh)

    def set_clf(self, **kw):
        self.clf = kw
        self.write_cfg()

    def route(self, fake, *extra, text=UNSURE, json_out=False):
        argv = (['--config', self.cfgpath] + (['--json'] if json_out else [])
                + ['route', text, '--repo', self.repo, *extra])
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env):
            with patch('mr.classifier.classify', new=fake):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_route_off_drops_not_built_line(self):
        fake = FakeClassifier()
        code, out, err = self.route(fake)
        self.assertNotIn(NOT_BUILT, err)
        self.assertEqual(fake.calls, [])
        self.assertEqual(_lines(out), [])

    def test_route_shadow_prints_classifier_line(self):
        fake = FakeClassifier()
        code_off, out_off, _ = self.route(fake, '--classifier', 'off')
        code_on, out_on, err = self.route(fake, '--classifier', 'shadow')
        lines = _lines(out_on)
        self.assertEqual(len(lines), 1, out_on + err)
        for part in ('type=ios-native', 'difficulty=critical', 'risk=high', 'route=design', 'conf=', 'status=ok'):
            self.assertIn(part, lines[0])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(code_on, code_off)
        self.assertEqual(_decision_lines(out_on), _decision_lines(out_off))
        self.assertNotIn(NOT_BUILT, err)

    def test_route_shadow_json_key(self):
        fake = FakeClassifier()
        code_off, out_off, _ = self.route(fake, '--classifier', 'off', json_out=True)
        code_on, out_on, err = self.route(fake, '--classifier', 'shadow', json_out=True)
        off, on = json.loads(out_off), json.loads(out_on)
        self.assertIsNone(off.get('classifier_shadow'))
        summ = on.get('classifier_shadow')
        self.assertIsInstance(summ, dict, err)
        self.assertEqual((summ['status'], summ['type'], summ['difficulty'], summ['route'], summ['confidence']),
                         ('ok', 'ios-native', 'critical', 'design', 1.0))
        self.assertEqual(code_on, code_off)
        for key in ('decision', 'features', 'reasons', 'exit', 'fingerprint'):
            self.assertEqual(on[key], off[key], key)

    def test_route_no_models_makes_no_call(self):
        fake = FakeClassifier()
        self.route(fake, '--classifier', 'shadow')
        self.assertEqual(len(fake.calls), 1)
        code, out, err = self.route(fake, '--classifier', 'shadow', '--no-models')
        code_j, out_j, err_j = self.route(fake, '--classifier', 'shadow', '--no-models', json_out=True)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(_lines(out), [])
        self.assertIsNone(json.loads(out_j).get('classifier_shadow'))
        self.assertNotIn(NOT_BUILT, err + err_j)

    def test_config_shadow_uses_should_run_gate(self):
        self.set_clf(mode='shadow', sample_n=0)
        fake = FakeClassifier()
        self.route(fake, *GIVEN, text=CONFIDENT)
        self.assertEqual(fake.calls, [])  # confident rules, sample_n 0: never sampled
        code, out, err = self.route(fake, text=UNSURE)
        self.assertEqual(len(fake.calls), 1, err)  # unsure rules: always consulted
        self.assertEqual(len(_lines(out)), 1)

    def test_cli_shadow_forces_classifier_run(self):
        self.set_clf(sample_n=0)
        fake = FakeClassifier()
        code, out, err = self.route(fake, '--classifier', 'shadow', *GIVEN, text=CONFIDENT)
        self.assertEqual(len(fake.calls), 1, err)
        self.assertEqual(len(_lines(out)), 1)

    def test_classifier_on_runs_as_shadow(self):
        fake = FakeClassifier()
        code, out, err = self.route(fake, '--classifier', 'on')
        self.assertIn(ON_LINE, err)
        self.assertEqual(err.count(ON_LINE), 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(_lines(out)), 1)

    def test_config_on_runs_as_shadow(self):
        self.set_clf(mode='on')
        fake = FakeClassifier()
        code, out, err = self.route(fake)
        self.assertIn(ON_LINE, err)
        self.assertEqual(len(fake.calls), 1)

    def test_judge_shadow_is_accepted_and_runs_with_the_classifier(self):
        fake = FakeClassifier()
        code, out, err = self.route(fake, '--judge', 'shadow', '--classifier', 'shadow')
        self.assertNotIn('judge not built', err)
        self.assertNotIn(NOT_BUILT, err)
        self.assertEqual(len([ln for ln in out.splitlines() if ln.startswith('judge (shadow):')]), 1, out)
        self.assertEqual(len(fake.calls), 1)


class ClassifierConfigKeysTest(unittest.TestCase):
    def test_defaults_have_sample_n_and_join_timeout_s(self):
        cfg = from_dict(copy.deepcopy(defaults()))
        self.assertEqual(cfg.classifier.get('sample_n'), 3)
        self.assertEqual(cfg.classifier.get('join_timeout_s'), 75)
        self.assertEqual(cfg.classifier.get('mode'), 'off')
        raw = copy.deepcopy(defaults())
        raw['classifier'].update(sample_n=0, join_timeout_s=0.5)
        cfg = from_dict(raw)
        self.assertEqual((cfg.classifier['sample_n'], cfg.classifier['join_timeout_s']), (0, 0.5))

    def test_rejects_bad_sample_n(self):
        raw = copy.deepcopy(defaults())
        raw['classifier']['sample_n'] = -1
        with self.assertRaises(ConfigError) as caught:
            from_dict(raw)
        self.assertIn('classifier.sample_n', str(caught.exception))
        raw = copy.deepcopy(defaults())
        raw['classifier']['sample_n'] = 'three'
        with self.assertRaises(ConfigError) as caught:
            from_dict(raw)
        self.assertIn('classifier.sample_n', str(caught.exception))

    def test_rejects_bad_join_timeout_s(self):
        raw = copy.deepcopy(defaults())
        raw['classifier']['join_timeout_s'] = 0
        with self.assertRaises(ConfigError) as caught:
            from_dict(raw)
        self.assertIn('classifier.join_timeout_s', str(caught.exception))
        raw = copy.deepcopy(defaults())
        raw['classifier']['join_timeout_s'] = -5
        with self.assertRaises(ConfigError) as caught:
            from_dict(raw)
        self.assertIn('classifier.join_timeout_s', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
