'''AJ5 locked tests (units and `mr route`): isolation inputs come from path evidence only, decide_isolation never
returns W4 while worktrees are not built (risk.high_isolation W4 keeps the old table), the new config keys, and a
design decision maps to cascade with an Opus plan and an S+ review unless design_route.map_to_cascade is false.
Fake claude and cursor-agent only; no network.'''
import contextlib
import copy
import dataclasses
import io
import itertools
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mr import cli, isolation
from mr.config import ConfigError, from_dict
from mr.defaults import defaults
from tests import gitfix
from tests.test_aj4_run_route_mr import EASY, OnCase
from tests.test_run_once import RunCase

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'p0', 'isolation_table.json')
OLD_KEYS = ('RO', 'OTHER_WRITER', 'DIRTY_FOREIGN', 'PARALLEL', 'EXPERIMENT', 'HIGH_RISK', 'DEPS', 'PROTECTED',
            'OWNED_BRANCH')
KEYS = OLD_KEYS + ('NEEDS_BRANCH',)
HIGH_SET = ('auth', 'payments', 'migration', 'data-deletion', 'deps', 'native-config')
TEXT_AUTH = "Fix the 'Authentication required' error text in src/ui/banner.ts"
CRITICAL = ('rewrite the cache layer in src/cache.py', '--type', 'code', '--difficulty', 'critical')
DESIGN_EXIT = 'design route not built; re-run with --route cascade or plan by hand'
MAPPED = 'design_mapped_to_cascade'


def iso_fields():
    return [f.name for f in dataclasses.fields(isolation.IsoInputs)]


def inputs(**kw):
    vals = dict.fromkeys(KEYS, False)
    vals.update(kw)
    if 'NEEDS_BRANCH' not in iso_fields():
        vals.pop('NEEDS_BRANCH')
    return isolation.IsoInputs(**vals)


class DecideIsolationTest(unittest.TestCase):
    def test_never_returns_w4_while_worktrees_are_not_built(self):
        self.assertIn('NEEDS_BRANCH', iso_fields())
        self.assertNotIn('W4', isolation.BUILT)
        seen = 0
        for combo in itertools.product((False, True), repeat=len(KEYS)):
            vals = dict(zip(KEYS, combo))
            got = isolation.decide_isolation(isolation.IsoInputs(**vals))
            self.assertNotEqual(got, 'W4', vals)
            early = any(vals[k] for k in ('RO', 'PARALLEL', 'OTHER_WRITER', 'DIRTY_FOREIGN', 'EXPERIMENT'))
            if not early and (vals['HIGH_RISK'] or vals['DEPS'] or vals['NEEDS_BRANCH']):
                self.assertEqual(got, 'W5', vals)
            seen += 1
        self.assertEqual(seen, 1024)

    def test_high_isolation_w4_keeps_the_old_table(self):
        self.assertEqual(isolation.decide_isolation(inputs(HIGH_RISK=True)), 'W5')
        self.assertEqual(isolation.decide_isolation(inputs(DEPS=True)), 'W5')
        for kw in ({'HIGH_RISK': True}, {'DEPS': True}, {'NEEDS_BRANCH': True}):
            self.assertEqual(isolation.decide_isolation(inputs(**kw), high_isolation='W4'), 'W4', kw)
        self.assertEqual(isolation.decide_isolation(inputs(), high_isolation='W4'), 'W7')
        with open(FIXTURE, encoding='utf-8') as fh:
            rows = json.load(fh)
        for row in rows:
            got = isolation.decide_isolation(isolation.IsoInputs(**row['inputs']), high_isolation='W4')
            self.assertEqual(got, row['expect'], row['note'])


class ComputeInputsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='mr-aj5-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = gitfix.make_repo(os.path.join(self.tmp, 'repo'), branch='feature/x')
        self.cfg = SimpleNamespace(paths=SimpleNamespace(home=os.path.join(self.tmp, 'home')),
                                   worktrees={'protected': ['main', 'master', 'release/*', 'production']})

    def inputs(self, **kw):
        base = dict(ro=False, risk='low', difficulty='easy', risk_tags=(), experiment=False, other_writer=False,
                    cfg=self.cfg)
        base.update(kw)
        return isolation.compute_inputs(self.repo, **base)

    def test_text_only_tags_never_set_high_risk(self):
        got = self.inputs(risk='high', risk_tags=list(HIGH_SET))
        self.assertFalse(got.HIGH_RISK, 'text-only tags with a final risk of high')
        self.assertFalse(got.DEPS, 'a text-only deps tag')
        for tag in HIGH_SET:
            self.assertTrue(self.inputs(risk='high', risk_tags=[tag], path_tags=[tag]).HIGH_RISK, tag)
        self.assertTrue(self.inputs(path_tags=['deps']).DEPS)
        self.assertFalse(self.inputs(risk='medium', risk_tags=['secrets'], path_tags=['secrets']).HIGH_RISK)
        self.assertEqual(isolation.decide_isolation(self.inputs(risk='high', risk_tags=['auth'])), 'W7')
        self.assertEqual(isolation.decide_isolation(
            self.inputs(risk='high', risk_tags=['auth'], path_tags=['auth'])), 'W5')

    def test_critical_or_large_change_needs_a_branch_not_high_risk(self):
        crit = self.inputs(difficulty='critical')
        self.assertFalse(crit.HIGH_RISK, 'difficulty critical')
        self.assertIs(getattr(crit, 'NEEDS_BRANCH', None), True)
        big = self.inputs(est_loc=400)
        self.assertFalse(big.HIGH_RISK, 'est_loc 400')
        self.assertIs(big.NEEDS_BRANCH, True)
        self.assertIs(self.inputs(est_loc=399, est_files=20).NEEDS_BRANCH, False)
        self.assertEqual(isolation.decide_isolation(crit), 'W5')
        self.assertEqual(isolation.decide_isolation(big), 'W5')


class ConfigKeysTest(unittest.TestCase):
    def test_defaults_and_validation_of_the_new_keys(self):
        raw = defaults()
        self.assertIs(raw['design_route'].get('map_to_cascade'), True)
        self.assertEqual((raw.get('risk') or {}).get('high_isolation'), 'W5')
        for bad in ('W9', 'W7', '', 4):
            r = defaults()
            r['risk'] = {'high_isolation': bad}
            with self.assertRaises(ConfigError, msg=repr(bad)):
                from_dict(r)
        for bad in ('yes', 1):
            r = defaults()
            r['design_route']['map_to_cascade'] = bad
            with self.assertRaises(ConfigError, msg=repr(bad)):
                from_dict(r)
        for good in ('W4', 'W5'):
            r = defaults()
            r['risk'] = {'high_isolation': good}
            from_dict(r)
        r = defaults()
        r['design_route']['map_to_cascade'] = False
        from_dict(r)


class RouteCase(RunCase):
    def setUp(self):
        super().setUp()
        self.env['MR_HOME'] = self.home

    def set_cfg(self, **blocks):
        with open(self.cfgpath, encoding='utf-8') as fh:
            raw = json.load(fh)
        raw.update(blocks)
        with open(self.cfgpath, 'w', encoding='utf-8') as fh:
            json.dump(raw, fh)

    def route(self, *args, json_out=False):
        argv = (['--config', self.cfgpath] + (['--json'] if json_out else [])
                + ['route', *args, '--repo', self.repo, '--no-models'])
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()


class RouteDesignTest(RouteCase):
    def test_text_only_auth_word_is_not_design(self):
        code, out, err = self.route(TEXT_AUTH, json_out=True)
        self.assertEqual(code, 0, err)
        self.assertNotIn(DESIGN_EXIT, err)
        doc = json.loads(out)
        self.assertEqual(doc['decision']['route'], 'cascade', out)
        self.assertNotIn(MAPPED, doc['reasons'])
        self.assertIn('auth', doc['decision']['risk_tags'])

    def test_design_cell_maps_to_cascade_with_opus_plus_plan_and_s_plus_review(self):
        code, out, err = self.route(*CRITICAL, json_out=True)
        self.assertEqual(code, 0, err)
        self.assertNotIn(DESIGN_EXIT, err)
        self.assertIn(MAPPED, err)
        doc = json.loads(out)
        d = doc['decision']
        self.assertEqual(d['route'], 'cascade', out)
        self.assertEqual(d['ladder'], ['G-x', 'S+', 'O'])
        self.assertEqual((d.get('planner'), d.get('required_review')), ('O+', 'S+'))
        self.assertIs(d['needs_review'], True)
        self.assertEqual(doc['reasons'].count(MAPPED), 1, doc['reasons'])

    def test_map_to_cascade_false_restores_exit_3(self):
        code, _out, err = self.route(*CRITICAL)
        self.assertEqual(code, 0, err)
        self.set_cfg(design_route={'map_to_cascade': False})
        code, out, err = self.route(*CRITICAL, json_out=True)
        self.assertEqual(code, 3, err)
        self.assertIn(DESIGN_EXIT, err)
        doc = json.loads(out)
        self.assertEqual((doc['decision']['route'], doc['decision']['ladder']), ('design', []))
        self.assertNotIn(MAPPED, doc['reasons'])


class JudgeDesignTest(OnCase):
    def test_judge_route_class_design_maps_to_cascade(self):
        reading = {'route_class': {'decision': 'raise', 'value': 'design', 'conf': 0.65, 'evidence': None,
                                   'quote_found': False},
                   'files': copy.deepcopy(EASY['files']), 'tags': {}}
        with self.judge_says(reading):
            code, out, err = self.route('fix typo in README.md', '--type', 'docs', '--difficulty', 'trivial',
                                        json_out=True)
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        d = doc['decision']
        self.assertEqual(d['route'], 'cascade', out)
        self.assertEqual(d['sources']['route'], 'judge')
        self.assertEqual(d['ladder'], ['G-l'])
        self.assertEqual((d.get('planner'), d.get('required_review')), ('O', 'S+'))
        self.assertIs(d['needs_review'], True)
        self.assertIn(MAPPED, doc['reasons'])


if __name__ == '__main__':
    unittest.main()
