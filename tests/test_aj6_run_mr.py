'''AJ6 locked tests (CLI): `mr run` with a task JSON that gives hints.files, or the deprecated files_named (one
stderr line and a deprecated_field note in the task record), the implementer prompt and task record files_src from
the decision files, signal D judge-only warnings in d_warnings with the judge on, and the planner prompt with file
sources and the planner scope as the final files. Fake cursor-agent and fake claude only; the judge answer parsing
is patched as in the AJ4 tests.'''
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

from mr import cli
from mr import exitcodes as ec
from tests.test_aj4_run_route_mr import HAIKU, OnCase
from tests.test_loop_run import GH, GX, PLAN, REVIEW_PASS, SONNET, LoopCase, mul
from tests.test_run_once import GL, RunCase

NL = chr(10)
TEXT = 'fix the typo in the readme'
JTEXT = 'fix typo in README.md and `helper_name`'
DEPRECATED = 'files_named is deprecated; use hints.files'
RESOLVER = "These files come from mr's resolver and the judge; add any file the change needs."
FIXED = '# test repo' + NL + 'fixed typo' + NL
PICK = {'files': {'change': ['src/util.py'], 'new': [], 'complete': True,
                  'conf': {'change': 0.9, 'new': None, 'complete': 0.9}}, 'tags': {}}


class HintsRunTest(RunCase):
    def run_file(self, doc):
        self.env['MR_HOME'] = self.home
        path = os.path.join(self.dir, 'task.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh)
        argv = ['--config', self.cfgpath, 'run', '-f', path, '--repo', self.repo, '--rung', 'G-l', '--once']
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def impl_prompt(self):
        prompts = [c.get('prompt') or '' for c in self.calls() if c.get('model') == GL]
        self.assertEqual(len(prompts), 1, self.calls())
        return prompts[0]

    def test_hints_files_task_file_scopes_the_run(self):
        self.cursor({'files': {'README.md': FIXED}})
        code, _out, err = self.run_file({'text': TEXT, 'repo': self.repo, 'hints': {'files': ['README.md']}})
        self.assertEqual(code, ec.OK, err)
        self.assertNotIn(DEPRECATED, err)
        self.assertIn('- README.md', self.impl_prompt())
        task = self.only_task()
        self.assertEqual(task.get('files_src'), {'hint': 1})
        self.assertIsNone(task.get('deprecated_field'))

    def test_files_named_task_file_is_deprecated_alias(self):
        self.cursor({'files': {'README.md': FIXED}})
        code, _out, err = self.run_file({'text': TEXT, 'repo': self.repo, 'files_named': ['README.md']})
        self.assertEqual(code, ec.OK, err)
        self.assertEqual(err.count(DEPRECATED), 1, err)
        task = self.only_task()
        self.assertEqual(task.get('deprecated_field'), 'files_named')
        self.assertEqual(task.get('files_src'), {'hint': 1})
        self.assertIn('- README.md', self.impl_prompt())


class JudgeFilesRunTest(OnCase):
    repo_files = {'src/util.py': 'helper_name = 1' + NL}

    def go(self, files):
        self.set_scenario({'cursor': {GL: [{'files': files}]}, 'claude': {HAIKU: [{'result': '{}'}] * 2}})
        self.env['MR_HOME'] = self.home
        with self.judge_says(PICK):
            return self.mr(text=JTEXT)

    def attempt(self):
        recs = self.records('implementer')
        self.assertEqual(len(recs), 1, recs)
        return recs[0]

    def test_judge_only_file_is_a_d_warning_not_a_failure(self):
        code, _out, err = self.go({'README.md': FIXED, 'src/util.py': 'helper_name = 2' + NL})
        self.assertEqual(code, ec.OK, err)
        att = self.attempt()
        self.assertIs(att['signals']['D'], True)
        warnings = att.get('d_warnings')
        self.assertIsInstance(warnings, list)
        self.assertTrue(any('src/util.py' in w for w in warnings), warnings)

    def test_unrelated_file_still_fails_with_no_d_warning(self):
        code, _out, err = self.go({'README.md': FIXED, 'lib/other.py': 'y = 1' + NL})
        self.assertEqual(code, ec.NOT_ACCEPTED, err)
        att = self.attempt()
        self.assertIs(att['signals']['D'], False)
        self.assertEqual(att.get('d_warnings'), [])


class PlannerRunTest(LoopCase):
    def test_planner_gets_decision_files_and_sets_planner_scope(self):
        self.set_scenario({'cursor': {GH: [{'result': json.dumps(PLAN)}], GX: [mul('+'), {'result': REVIEW_PASS}]},
                           'claude': {SONNET: [mul('*', '  # fixed')]}})
        self.env['MR_HOME'] = self.home
        _code, _doc, err = self.run_json('--type', 'bugfix', '--difficulty', 'medium')
        prompts = self.prompts(GH)
        self.assertTrue(prompts, err)
        self.assertIn('- README.md (named)', prompts[0])
        self.assertIn(RESOLVER, prompts[0])
        self.assertEqual(self.only_task().get('files_src'), {'planner': 3})


if __name__ == '__main__':
    unittest.main()
