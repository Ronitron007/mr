'''AJ1 locked tests: mr.facts stage A (candidates, ranking, caps), risk evidence, sensitive paths and type
features on temp git repos. No network, no model calls.'''
import dataclasses
import importlib
import os
import shutil
import tempfile
import unittest

from mr import prepass
from tests import gitfix


def _facts():
    return importlib.import_module('mr.facts')


def _paths(f):
    return [c.path for c in f.candidates]


def _cand(f, path):
    hits = [c for c in f.candidates if c.path == path]
    return hits[0] if hits else None


class FactsCase(unittest.TestCase):
    files = {}

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='mr-aj1-'))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = os.path.join(self.dir, 'home')
        os.makedirs(self.home)
        old = os.environ.get('MR_HOME')
        os.environ['MR_HOME'] = self.home
        self.addCleanup(self._restore, old)
        self.repo = os.path.join(self.dir, 'repo')
        gitfix.make_repo(self.repo, dict(self.files))

    @staticmethod
    def _restore(old):
        if old is None:
            os.environ.pop('MR_HOME', None)
        else:
            os.environ['MR_HOME'] = old

    def build(self, text, hints=()):
        return _facts().build_facts(text, self.repo, hints=hints)


class StageATest(FactsCase):
    files = {
        'src/app/main.py': 'def run():\n    return 1\n',
        'src/app/store.py': 'def load_store():\n    return {}\n',
        'tests/test_store.py': 'from src.app.store import load_store\n',
        'src/ui/Button.tsx': 'export const Button = 1;\n',
        'src/ui/Button.test.tsx': 'test("b", () => {});\n',
        'docs/a.md': 'a\n', 'docs/b.md': 'b\n', 'docs/c.txt': 'c\n',
        **{'pkg/f%02d.py' % i: 'x = %d\n' % i for i in range(25)},
        **{'lib/m%02d.py' % i: 'shared_helper = %d\n' % i for i in range(12)},
        **{'%s/f%02d.txt' % (d, i): '%s%d\n' % (d, i) for d in ('a', 'b', 'c') for i in range(21)},
    }

    def test_dataclass_fields(self):
        mod = _facts()
        names = lambda cls: [fl.name for fl in dataclasses.fields(cls)]
        self.assertEqual(names(mod.Candidate), ['path', 'exists', 'why'])
        self.assertEqual(names(mod.RiskItem), ['tag', 'source', 'quote', 'path'])
        self.assertEqual(names(mod.Facts), ['path_tokens', 'paths_exist', 'candidates', 'risk_evidence',
                                            'sensitive_paths', 'type_features', 'repo_files'])

    def test_named_existing_and_new(self):
        f = self.build('update src/app/main.py and add src/app/new_mod.py')
        self.assertEqual(f.path_tokens[:2], ('src/app/main.py', 'src/app/new_mod.py'))
        self.assertIs(f.paths_exist['src/app/main.py'], True)
        self.assertIs(f.paths_exist['src/app/new_mod.py'], False)
        old, new = _cand(f, 'src/app/main.py'), _cand(f, 'src/app/new_mod.py')
        self.assertTrue(old.exists)
        self.assertIn('named', old.why)
        self.assertIn('exists', old.why)
        self.assertFalse(new.exists)
        self.assertIn('named', new.why)
        self.assertIn('new', new.why)
        self.assertEqual(f.repo_files, len(self.files) + 2)

    def test_directory_token_expands_capped_20(self):
        f = self.build('clean up pkg/ please')
        pkg = [c for c in f.candidates if c.path.startswith('pkg/')]
        self.assertEqual(len(pkg), 20, _paths(f))
        for c in pkg:
            self.assertIn('glob', c.why, c)
            self.assertTrue(c.exists, c)
        self.assertNotIn('pkg/', _paths(f))

    def test_glob_token_expands(self):
        f = self.build('proofread docs/*.md today')
        docs = sorted(p for p in _paths(f) if p.startswith('docs/'))
        self.assertEqual(docs, ['docs/a.md', 'docs/b.md'])
        self.assertIn('glob', _cand(f, 'docs/a.md').why)

    def test_symbol_grep_finds_defining_file_and_sibling_test(self):
        f = self.build('fix the crash in `load_store` when the cache is empty')
        store = _cand(f, 'src/app/store.py')
        self.assertIsNotNone(store, _paths(f))
        self.assertIn('symbol:load_store', store.why)
        self.assertTrue(store.exists)
        sib = _cand(f, 'tests/test_store.py')
        self.assertIsNotNone(sib, _paths(f))
        self.assertTrue('sibling_test' in sib.why or 'symbol:load_store' in sib.why, sib)
        self.assertLess(_paths(f).index('src/app/store.py'), _paths(f).index('tests/test_store.py'))

    def test_sibling_ts_test_beside_named_file(self):
        f = self.build('restyle src/ui/Button.tsx for dark backgrounds')
        sib = _cand(f, 'src/ui/Button.test.tsx')
        self.assertIsNotNone(sib, _paths(f))
        self.assertIn('sibling_test', sib.why)
        self.assertEqual(_paths(f)[0], 'src/ui/Button.tsx')

    def test_symbol_grep_caps_10_files(self):
        f = self.build('rename `shared_helper` everywhere')
        sym = [c for c in f.candidates if 'symbol:shared_helper' in c.why]
        self.assertEqual(len(sym), 10, _paths(f))

    def test_rank_named_first_dedupe_and_cap_40(self):
        f = self.build('touch a/ b/ c/ then fix src/app/main.py and a/f00.txt')
        paths = _paths(f)
        self.assertEqual(len(paths), 40, paths)
        self.assertEqual(len(set(paths)), 40, paths)
        self.assertEqual(set(paths[:2]), {'src/app/main.py', 'a/f00.txt'})
        both = _cand(f, 'a/f00.txt')
        self.assertIn('named', both.why)
        self.assertIn('glob', both.why)
        for c in f.candidates[2:]:
            self.assertIn('glob', c.why, c)

    def test_outside_paths_dropped(self):
        outside = os.path.join(self.dir, 'elsewhere', 'x.py')
        f = self.build(f'compare ../outside.py with {outside} and src/app/main.py')
        paths = _paths(f)
        self.assertIn('src/app/main.py', paths)
        for p in paths:
            self.assertFalse(p.startswith('/') or '..' in p.split('/'), p)

    def test_non_git_dir_never_raises(self):
        mod = _facts()
        plain = os.path.join(self.dir, 'plain')
        os.makedirs(plain)
        for where in (plain, os.path.join(self.dir, 'missing')):
            f = mod.build_facts('update src/a.py using `load_store` and pkg/', where)
            self.assertEqual(f.repo_files, 0, where)
            for c in f.candidates:
                self.assertFalse(c.exists, (where, c))
                self.assertTrue(set(c.why) <= {'named', 'hint', 'new'}, (where, c))


class RiskEvidenceTest(FactsCase):
    files = {'src/auth/login.ts': 'export const login = 1;\n', 'README.md': '# r\n'}

    def test_text_item_with_quote_and_no_path_item(self):
        text = 'fix the Authentication required error message'
        f = self.build(text)
        auth = [r for r in f.risk_evidence if r.tag == 'auth' and r.source == 'text']
        self.assertEqual(len(auth), 1, f.risk_evidence)
        self.assertEqual(auth[0].quote, 'Authentication')
        self.assertIsNone(auth[0].path)
        self.assertEqual([r for r in f.risk_evidence if r.source == 'path'], [])
        cases = ((text, frozenset({'auth'})),
                 ('add supabase/migrations/0042_x.sql', frozenset({'migration'})),
                 ('rotate the secret in src/auth/login.ts', frozenset({'auth', 'secrets'})),
                 ('fix typo in README.md', frozenset()))
        for t, want in cases:
            self.assertEqual(prepass.risk_tags(prepass.prose(t), prepass.path_tokens(t)), want, t)

    def test_new_migration_path_token_gives_path_item(self):
        f = self.build('add supabase/migrations/0042_x.sql for orders')
        items = [(r.tag, r.path, r.quote) for r in f.risk_evidence if r.source == 'path']
        self.assertIn(('migration', 'supabase/migrations/0042_x.sql', None), items)

    def test_existing_auth_path_gives_path_item(self):
        f = self.build('tidy src/auth/login.ts')
        items = [(r.tag, r.path) for r in f.risk_evidence if r.source == 'path']
        self.assertIn(('auth', 'src/auth/login.ts'), items)

    def test_sensitive_word_or_new_hint_gives_no_path_item(self):
        f = self.build('update the password helper', hints=('src/auth/new_flow.ts',))
        self.assertIn('src/auth/new_flow.ts', _paths(f))
        self.assertEqual([r for r in f.risk_evidence if r.source == 'path'], [])
        self.assertTrue(any(r.tag == 'auth' and r.source == 'text' and r.quote == 'password'
                            for r in f.risk_evidence), f.risk_evidence)


class TypeFeaturesTest(FactsCase):
    files = {'tests/test_a.py': 'x = 1\n', 'src/a.py': 'y = 1\n'}

    def test_lead_verb(self):
        cases = (('Write tests for x', 'write'), ('Implement y', 'implement'),
                 ('Hello there.', None))
        for text, want in cases:
            self.assertEqual(self.build(text).type_features['lead_verb'], want, text)

    def test_test_only_scope(self):
        cases = (('update tests/test_a.py', True), ('update tests/test_a.py and src/a.py', False),
                 ('update src/a.py', False), ('update the notes', False))
        for text, want in cases:
            self.assertIs(self.build(text).type_features['test_only_scope'], want, text)

    def test_keyword_hits_only_first_300_chars(self):
        late = 'Implement the parser. ' + 'x ' * 200 + 'test tests testing coverage'
        hits = self.build(late).type_features['keyword_hits']
        self.assertEqual(hits['test'], 0, hits)
        self.assertEqual(hits['code'], 1, hits)
        early = self.build('Write tests for the parser').type_features['keyword_hits']
        self.assertEqual(early['test'], 1, early)


class SensitivePathsTest(FactsCase):
    files = {'.env.local': 'A=1\n', 'src/a.py': 'y = 1\n'}

    def test_sensitive_paths(self):
        self.assertEqual(self.build('update .env.local and src/a.py').sensitive_paths, ('.env.local',))
        self.assertEqual(self.build('rotate the secret value in src/a.py').sensitive_paths, ())
        got = self.build('add supabase/migrations/0042_x.sql and fix .env.local').sensitive_paths
        self.assertEqual(got, ('.env.local', 'supabase/migrations/0042_x.sql'))


if __name__ == '__main__':
    unittest.main()
