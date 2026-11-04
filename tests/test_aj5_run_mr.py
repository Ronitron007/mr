'''AJ5 locked tests (`mr run` with fake runners): a text-only risk word runs on W7 at medium risk; a path auth task
runs on a W5 branch with an O plan and an S+ review and stays unmerged; a design cell runs as cascade (O+ plan, S+
review) and exits 3 only with design_route.map_to_cascade false; a guard-denied strong review on a high-risk task
defers (exit 75) while low risk keeps the C25 fallback; deps and migration paths add the prompt line and the
human-go message. No network, no real claude, cursor-agent or check.sh.'''
import json
import os
import unittest

from tests import gitfix
from tests.gitfix import git
from tests.test_aj4_run_route_mr import OnCase
from tests.test_loop_run import GH, GX, OPUS, PLAN, REVIEW_PASS, SONNET, LoopCase, mul
from tests.test_run_once import CHECK_DEFAULT, GL, RunCase

C25 = 'composer-2.5'
MAPPED = 'design_mapped_to_cascade'
DESIGN_EXIT = 'design route not built; re-run with --route cascade or plan by hand'
TEXT_AUTH = "Fix the 'Authentication required' error text in src/ui/banner.ts"
PATH_AUTH = 'Fix the login check in src/auth/login.ts'
LOGIN = {'src/auth/login.ts': 'export const login = 1;\n'}
HAND = 'review and merge by hand'
HUMAN_GO = 'migrations need a human go before any migration command runs'


def strong_scenario():
    return {'cursor': {GX: [mul('*')]},
            'claude': {OPUS: [{'result': json.dumps(PLAN)}], SONNET: [{'result': REVIEW_PASS}]}}


class AJ5Loop(LoopCase):
    def rungs(self, role):
        return [r['rung'] for r in self.records(role)]

    def route_rec(self, index=0):
        return self.records('route')[index]

    def back_to_branch(self):
        git(self.repo, 'checkout', '-q', '-f', self.branch)
        git(self.repo, 'clean', '-q', '-f', '-d')

    def fresh(self, scenario, pct=30.0):
        for suffix in ('.state.json', '.calls.jsonl'):
            path = os.path.join(self.dir, 'scenario.json' + suffix)
            if os.path.exists(path):
                os.remove(path)
        self.set_scenario(scenario, check={'default': dict(CHECK_DEFAULT, five_hour_pct=pct)})


class TextOnlyRunTest(OnCase):
    repo_files = {'src/ui/banner.ts': "export const msg = 'Authentication required';\n"}

    def test_text_only_auth_runs_on_w7_at_medium_risk(self):
        self.set_scenario({'cursor': {GL: [{'files': {'src/ui/banner.ts': "export const msg = 'Sign in first';\n"}}]}})
        self.env['MR_HOME'] = self.home
        with self.judge_fails():
            code, out, err = self.mr('--classifier', 'off', text=TEXT_AUTH, json_out=True)
        self.assertEqual(code, 0, err)
        self.assertNotIn(DESIGN_EXIT, err)
        route = self.route_rec()
        self.assertIs(route.get('judge_absent'), True, route)
        self.assertEqual(route['applied']['risk'], 'medium', route)
        self.assertIs(route.get('design_mapped_to_cascade'), False, route)
        iso = route.get('isolation_inputs') or {}
        self.assertIs(iso.get('HIGH_RISK'), False, route)
        self.assertEqual(iso.get('from_tags'), [], route)
        self.assertEqual(self.state_of(json.loads(out)['task'])['isolation'], 'W7')
        self.assertEqual(self.only_task()['outcome'], 'accepted')


class HighRiskRunTest(AJ5Loop):
    repo_files = {**LoopCase.repo_files, **LOGIN}

    def test_path_auth_runs_on_a_w5_branch_with_strong_review(self):
        base = gitfix.head(self.repo)
        self.fresh(strong_scenario())
        code, doc, err = self.run_json('--type', 'bugfix', '--difficulty', 'hard', text=PATH_AUTH)
        self.assertEqual(code, 0, err)
        self.assertTrue(str(doc.get('branch')).startswith('mr/'), doc)
        self.assertEqual(self.state_of(doc['task'])['isolation'], 'W5')
        self.assertEqual(git(self.repo, 'rev-parse', self.branch).stdout.strip(), base, 'the branch was merged')
        self.assertEqual(git(self.repo, 'symbolic-ref', '--short', 'HEAD').stdout.strip(), doc['branch'])
        self.assertIn(HAND, err)
        self.assertEqual(self.rungs('planner'), ['O'])
        self.assertEqual(self.rungs('reviewer'), ['S+'])
        route = self.route_rec()
        self.assertEqual(route.get('required_review'), 'S+', route)
        iso = route.get('isolation_inputs') or {}
        self.assertIs(iso.get('HIGH_RISK'), True, route)
        self.assertIn('auth', iso.get('from_tags') or [], route)

    def test_denied_strong_review_defers_on_high_risk_and_low_risk_keeps_c25(self):
        scenario = {'cursor': {GH: [{'result': json.dumps(PLAN)}], GX: [mul('*')], C25: [{'result': REVIEW_PASS}]},
                    'claude': {OPUS: [{}], SONNET: [{}]}}
        self.fresh(scenario, pct=84.0)
        code, doc, err = self.run_json('--type', 'bugfix', '--difficulty', 'hard', text=PATH_AUTH)
        self.assertEqual(code, 75, err)
        self.assertEqual(doc.get('outcome'), 'deferred', doc)
        self.assertNotIn(C25, self.models(), err)
        self.assertTrue(doc.get('patch') and os.path.isfile(doc['patch']), doc)
        with open(doc['patch'], encoding='utf-8') as fh:
            self.assertIn('a * b', fh.read())
        self.back_to_branch()
        self.fresh(scenario, pct=84.0)
        code, doc, err = self.run_json('--type', 'bugfix', '--difficulty', 'medium')
        self.assertEqual(code, 0, err)
        self.assertIn(C25, self.models(), err)
        self.assertEqual(self.rungs('reviewer')[-1:], ['C25'])


class DesignCellRunTest(AJ5Loop):
    def test_design_cell_runs_as_cascade_and_exit_3_only_when_mapping_is_off(self):
        self.fresh(strong_scenario())
        code, doc, err = self.run_json('--type', 'code', '--difficulty', 'critical')
        self.assertEqual(code, 0, err)
        self.assertIn(MAPPED, err)
        self.assertEqual((doc['route'], doc['ladder']), ('cascade', ['G-x', 'S+', 'O']))
        self.assertEqual(self.rungs('planner'), ['O+'])
        self.assertEqual(self.rungs('reviewer'), ['S+'])
        route = self.route_rec()
        self.assertIs(route.get('design_mapped_to_cascade'), True, route)
        self.assertEqual(route.get('required_review'), 'S+', route)
        self.assertIs((route.get('isolation_inputs') or {}).get('NEEDS_BRANCH'), True, route)
        self.back_to_branch()
        with open(self.cfgpath, encoding='utf-8') as fh:
            raw = json.load(fh)
        raw['design_route'] = {'map_to_cascade': False}
        with open(self.cfgpath, 'w', encoding='utf-8') as fh:
            json.dump(raw, fh)
        calls = len(self.calls())
        code, doc, err = self.run_json('--type', 'code', '--difficulty', 'critical')
        self.assertEqual(code, 3, err)
        self.assertIn(DESIGN_EXIT, err)
        self.assertEqual(len(self.calls()), calls)


class DepsMigrationRunTest(RunCase):
    repo_files = {'package.json': '{"name": "x", "scripts": {"start": "node a.js"}}\n',
                  'supabase/migrations/0001_init.sql': 'create table t (id int);\n'}

    def test_deps_and_migration_paths_add_the_prompt_line_and_the_human_go_message(self):
        self.env['MR_HOME'] = self.home
        self.cursor({'files': {'package.json': '{"name": "x", "scripts": {"start": "node b.js"}}\n'}})
        text = 'Fix the start script in package.json and the comment in supabase/migrations/0001_init.sql'
        code, out, err = self.mr(text=text, json_out=True)
        self.assertEqual(code, 0, err)
        prompts = [c['prompt'] for c in self.calls() if c['model'] == GL]
        self.assertEqual(len(prompts), 1, self.calls())
        self.assertIn('do not install', prompts[0].lower())
        self.assertIn(HUMAN_GO, err)
        self.assertIn(HAND, err)
        doc = json.loads(out)
        self.assertTrue(str(doc.get('branch')).startswith('mr/'), doc)
        self.assertEqual(self.state_of(doc['task'])['isolation'], 'W5')


if __name__ == '__main__':
    unittest.main()
