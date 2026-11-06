'''AJ6 locked tests (unit): task JSON hints.files with files_named as a deprecated alias, no mr module reads
.files_named outside mr/task.py, Decision.files from facts (judge off) and from the merge (judge on), the
decision_files accessor and the trusted/judge-only split, the planner prompt with file sources, the planner scope as
the final files, and signal D judge-only warnings. No network; MR_HOME is a temp dir.'''
import importlib
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from mr import facts as facts_mod
from mr import patches, planner, prepass, router, scope
from mr.config import from_dict
from mr.defaults import defaults
from mr.facts import Candidate, Facts
from mr.task import Params, Task, TaskError, load_task, make_task
from tests import gitfix

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NL = chr(10)
README_FIXED = '# test repo' + NL + 'fixed typo' + NL
RESOLVER = "These files come from mr's resolver and the judge; add any file the change needs."
RULES = dict(route='cascade', type='code', difficulty='medium', risk='low', risk_tags=frozenset(),
             load_bearing=False, high_judgment=False, needs_review=True, isolate_hint=None,
             needs_clarification=False, ladder=('G-h', 'G-x', 'S'), reasons=(),
             sources={'route': 'default', 'type': 'rule', 'difficulty': 'rule', 'risk': 'rule'})


def pairs(entries):
    return [(e['path'], e['src']) for e in entries]


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='mr-aj6-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, 'home')
        env = patch.dict(os.environ, {'MR_HOME': self.home})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = from_dict(defaults())
        self.n = 0

    def repo(self, files=None):
        self.n += 1
        return gitfix.make_repo(os.path.join(self.root, 'repo%d' % self.n), files)

    def load(self, doc):
        path = os.path.join(self.root, 'task.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(doc, fh)
        return load_task(None, path, Params(), None, home=self.home)

    def fn(self, mod, name):
        got = getattr(mod, name, None)
        self.assertTrue(callable(got), '%s.%s is missing' % (mod.__name__, name))
        return got


class TaskSchemaTest(_Base):
    def test_hints_files_loaded_from_task_file(self):
        repo = self.repo({'src/a.py': 'x = 1' + NL})
        task = self.load({'text': 'Fix the parser', 'repo': repo, 'hints': {'files': ['src/a.py', 'src/b.py']}})
        self.assertEqual(getattr(task, 'hint_files', None), ('src/a.py', 'src/b.py'))
        self.assertEqual(task.files_named, ('src/a.py', 'src/b.py'))
        self.assertEqual(getattr(task, 'deprecated', None), ())

    def test_files_named_is_a_deprecated_alias_for_hints_files(self):
        repo = self.repo()
        task = self.load({'text': 'Fix the parser', 'repo': repo, 'files_named': ['src/a.py']})
        self.assertEqual(getattr(task, 'hint_files', None), ('src/a.py',))
        self.assertEqual(getattr(task, 'deprecated', None), ('files_named',))
        self.assertEqual(task.files_named, ('src/a.py',))

    def test_no_path_tokens_copied_into_the_task(self):
        repo = self.repo()
        task = self.load({'text': 'Fix src/a.ts and README.md', 'repo': repo})
        self.assertEqual(task.files_named, ())
        self.assertEqual(getattr(task, 'hint_files', None), ())
        made = make_task('Fix the typo in README.md', repo, gitfix.head(repo), Params())
        self.assertEqual(made.files_named, ())
        found = facts_mod.build_facts(made.text, repo, hints=made.files_named)
        self.assertEqual([c.path for c in found.candidates if 'named' in c.why], ['README.md'])

    def test_hints_files_must_be_a_list_of_strings(self):
        repo = self.repo()
        for bad in ({'files': 'README.md'}, {'files': [1]}, ['README.md'], 'README.md'):
            with self.assertRaises(TaskError, msg=repr(bad)):
                self.load({'text': 'Fix it', 'repo': repo, 'hints': bad})

    def test_files_named_is_a_read_only_property(self):
        self.assertIsInstance(Task.__dict__.get('files_named'), property)
        task = Task(id='t-0000aj06', text='Fix it', repo='/work/app', base='0' * 40, params=Params(),
                    files_named=('README.md',), source='cli')
        self.assertEqual(getattr(task, 'hint_files', None), ('README.md',))
        self.assertEqual(task.files_named, ('README.md',))


class StaticTest(_Base):
    def test_no_mr_module_reads_files_named_outside_task_py(self):
        hits = []
        for dirpath, _dirs, names in os.walk(os.path.join(ROOT, 'mr')):
            for name in sorted(names):
                if not name.endswith('.py'):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, ROOT).replace(os.sep, '/')
                if rel == 'mr/task.py':
                    continue
                with open(path, encoding='utf-8') as fh:
                    for num, line in enumerate(fh, 1):
                        if '.files_named' in line:
                            hits.append('%s:%d' % (rel, num))
        self.assertEqual(hits, [])

    def test_changelog_mentions_hints_files(self):
        with open(os.path.join(ROOT, 'CHANGELOG.md'), encoding='utf-8') as fh:
            body = fh.read()
        self.assertIn('## Unreleased', body)
        unreleased = body.split('## Unreleased', 1)[1].split(NL + '## ', 1)[0]
        self.assertIn('hints.files', unreleased)
        self.assertIn('files_named', unreleased)


class DecisionFilesTest(_Base):
    def test_judge_off_files_come_from_facts_alone(self):
        repo = self.repo({'src/app.py': 'x = 1' + NL})
        text = 'Fix README.md and add src/new_mod.py'
        task = make_task(text, repo, gitfix.head(repo), Params(), files_named=('src/app.py',))
        found = facts_mod.build_facts(text, repo, hints=('src/app.py',))
        d = router.decide(task, prepass.extract_from(text, None), self.cfg, facts=found)
        files = d.files or {}
        self.assertEqual(pairs(files.get('change') or []), [('README.md', 'named'), ('src/app.py', 'hint')])
        self.assertEqual(pairs(files.get('new') or []), [('src/new_mod.py', 'named')])
        self.assertEqual(files.get('read'), [])
        self.assertIsInstance(files.get('partial'), bool)
        decision_files = self.fn(router, 'decision_files')
        self.assertEqual(decision_files(d), ['README.md', 'src/app.py', 'src/new_mod.py'])

    def test_decision_files_accessor_and_scope_split(self):
        decision_files = self.fn(router, 'decision_files')
        scope_files = self.fn(router, 'scope_files')
        files = {'change': [{'path': 'README.md', 'src': 'named'}, {'path': 'src/util.py', 'src': 'judge'}],
                 'new': [{'path': 'src/x.py', 'src': 'hint'}, {'path': 'mul.py', 'src': 'planner'},
                         {'path': 'src/y.py', 'src': 'judge'}],
                 'read': [], 'partial': False}
        d = router.Decision(**RULES, files=files)
        want = ['README.md', 'src/util.py', 'src/x.py', 'mul.py', 'src/y.py']
        self.assertEqual(decision_files(d), want)
        self.assertEqual(decision_files(files), want)
        self.assertEqual(decision_files(router.Decision(**RULES)), [])
        trusted, judge_only = scope_files(d)
        self.assertEqual(list(trusted), ['README.md', 'src/x.py', 'mul.py'])
        self.assertEqual(list(judge_only), ['src/util.py', 'src/y.py'])

    def test_merge_files_keep_hint_new_and_read_key(self):
        merge = importlib.import_module('mr.judge.merge')
        cands = (Candidate('src/app.py', True, ('named', 'exists')),
                 Candidate('src/caller.py', True, ('hint', 'exists')),
                 Candidate('src/util.py', True, ('symbol:check_token',)),
                 Candidate('src/new_mod.py', False, ('named', 'new')),
                 Candidate('src/hint_new.py', False, ('hint', 'new')))
        facts = Facts(path_tokens=('src/app.py', 'src/new_mod.py'), paths_exist={c.path: c.exists for c in cands},
                      candidates=cands, risk_evidence=(), sensitive_paths=(),
                      type_features={'lead_verb': 'fix', 'test_only_scope': False, 'keyword_hits': {}},
                      repo_files=40)
        task = Task(id='t-0000aj06', text='Fix the check in src/app.py', repo=self.root, base='0' * 40,
                    params=Params(), files_named=(), source='cli')

        def reading(conf):
            return {'files': {'change': ['src/util.py'], 'new': [], 'complete': True,
                              'conf': {'change': conf, 'new': None, 'complete': None}}, 'tags': {}}

        m = merge.merge(task, facts, router.Decision(**RULES), reading(0.8), self.cfg)
        self.assertEqual(pairs(m.decision.files['change']),
                         [('src/app.py', 'named'), ('src/caller.py', 'hint'), ('src/util.py', 'judge')])
        self.assertEqual(pairs(m.decision.files['new']),
                         [('src/new_mod.py', 'named'), ('src/hint_new.py', 'hint')])
        self.assertEqual(m.decision.files.get('read'), [])
        self.assertEqual(m.decision.files, m.files)
        low = merge.merge(task, facts, router.Decision(**RULES), reading(0.7), self.cfg)
        decision_files = self.fn(router, 'decision_files')
        self.assertEqual(decision_files(low.decision),
                         ['src/app.py', 'src/caller.py', 'src/new_mod.py', 'src/hint_new.py'])


class PlannerFilesTest(_Base):
    def test_build_prompt_lists_decision_files_with_src(self):
        files = {'change': [{'path': 'src/a.py', 'src': 'judge'}], 'new': [{'path': 'src/b.py', 'src': 'named'}],
                 'read': [], 'partial': False}
        for runner in ('unittest', 'vitest'):
            body = planner.build_prompt('add b', files, 'README.md', 't-0000aj06', runner)
            self.assertIn('- src/a.py (judge)', body, runner)
            self.assertIn('- src/b.py (named, new)', body, runner)
            self.assertIn(RESOLVER, body, runner)

    def test_files_from_plan_are_the_final_scope(self):
        files_from_plan = self.fn(router, 'files_from_plan')
        checklist = {'scope': {'modify': ['README.md'], 'create': ['mul.py', 'tests/test_mul_mr.py'],
                               'forbidden': []}}
        self.assertEqual(files_from_plan(checklist), {
            'change': [{'path': 'README.md', 'src': 'planner'}],
            'new': [{'path': 'mul.py', 'src': 'planner'}, {'path': 'tests/test_mul_mr.py', 'src': 'planner'}],
            'read': [], 'partial': False})


class SignalDTest(_Base):
    def diff(self, before, after):
        repo = self.repo(before)
        reset = patches.take_reset_tree(repo)
        for rel, text in after.items():
            path = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(text)
        return patches.cumulative_diff(repo, reset)

    def d(self, patch_bytes, judge):
        try:
            return scope.scope(patch_bytes, checklist=None, difficulty='easy', files_named=('README.md',),
                               judge_files=judge)
        except TypeError as exc:
            self.fail('scope.scope has no judge_files: %s' % exc)

    def test_judge_only_file_outside_named_set_is_a_warning(self):
        diff = self.diff({'src/util.py': 'x = 1' + NL}, {'README.md': README_FIXED, 'src/util.py': 'x = 2' + NL})
        sig = self.d(diff, ('src/util.py',))
        self.assertTrue(sig.ok, sig.reasons)
        warnings = tuple(getattr(sig, 'warnings', ()) or ())
        self.assertTrue(any('src/util.py' in w for w in warnings), warnings)

    def test_named_file_passes_and_unrelated_file_still_fails(self):
        sig = self.d(self.diff({}, {'README.md': README_FIXED}), ('src/util.py',))
        self.assertTrue(sig.ok, sig.reasons)
        self.assertEqual(tuple(sig.warnings), ())
        diff = self.diff({'src/util.py': 'x = 1' + NL}, {'README.md': README_FIXED, 'lib/other.py': 'y = 1' + NL})
        bad = self.d(diff, ('src/util.py',))
        self.assertFalse(bad.ok)
        self.assertIn('out of scope: lib/other.py', bad.reasons)
        self.assertEqual(tuple(bad.warnings), ())

    def test_judge_only_sensitive_file_still_fails(self):
        diff = self.diff({'config/.env.local': 'A=1' + NL},
                         {'README.md': README_FIXED, 'config/.env.local': 'A=2' + NL})
        sig = self.d(diff, ('config/.env.local',))
        self.assertFalse(sig.ok)
        self.assertIn('sensitive path: config/.env.local', sig.reasons)


if __name__ == '__main__':
    unittest.main()
