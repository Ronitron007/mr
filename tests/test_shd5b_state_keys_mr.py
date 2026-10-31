'''SHD5b locked tests: the judge route state keys files_exist by the redacted file name.'''
import importlib
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest.mock import patch

from mr.prepass import extract_from
from mr.task import Params, Task

TEXT = 'Fix the typo in README.md heading'
# Fake secrets are built by concatenation so gitleaks and the public-tree gate never see a literal.
TOKEN_A = 'sk' + '-' + 'Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56Qr78St90'
TOKEN_B = 'gh' + 'p_' + 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8'
MAIL = 'qa.lead' + '@' + 'example.net'
MARK = 'src/<redacted:token>.py'


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


class FilesExistKeyTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = os.path.join(self.root, 'repo')
        os.makedirs(os.path.join(self.repo, 'src'))
        with open(os.path.join(self.repo, 'README.md'), 'w', encoding='utf-8') as fh:
            fh.write('# demo\n')
        home = os.path.join(self.root, 'home')
        os.makedirs(home)
        env = patch.dict(os.environ, {'MR_HOME': home})
        env.start()
        self.addCleanup(env.stop)

    def _build(self, files):
        st = importlib.import_module('mr.judge.state')
        qs = importlib.import_module('mr.judge.questions').load()
        task = Task(id='t-0000abcd', text=TEXT, repo=self.repo, base='abc1234', params=Params(),
                    files_named=tuple(files), source='arg')
        feat = extract_from(TEXT, files)
        return st.build_route_state(task, feat, types.SimpleNamespace(judge=None), 'demo', qs, env={})

    def test_files_exist_keys_match_redacted_names(self):
        files = ('src/' + TOKEN_A + '.py', 'docs/' + MAIL + '.md', 'README.md')
        b = self._build(files)
        dump = _canon(b.state)
        self.assertNotIn(TOKEN_A, dump)
        self.assertNotIn(MAIL, dump)
        facts = b.state['facts']
        self.assertEqual(facts['files_named'][0], MARK)
        self.assertIn('<redacted:email>', facts['files_named'][1])
        self.assertEqual(facts['files_named'][2], 'README.md')
        self.assertEqual(list(facts['files_exist']), facts['files_named'])
        self.assertIs(facts['files_exist']['README.md'], True)
        self.assertIs(facts['files_exist'][MARK], False)

    def test_key_redactions_are_counted(self):
        files = ('src/' + TOKEN_A + '.py', 'docs/' + MAIL + '.md')
        b = self._build(files)
        self.assertEqual(b.redactions, {'token': 2, 'email': 2})
        self.assertIsNone(b.status)

    def test_key_redactions_count_toward_abort(self):
        files = tuple('docs/u%d' % i + '@' + 'example.org.md' for i in range(13))
        b = self._build(files)
        self.assertEqual(b.redactions, {'email': 26})
        self.assertEqual(b.status, 'redaction_abort')
        self.assertNotIn('example.org', _canon(b.state))

    def test_colliding_redacted_names_keep_one_true_entry(self):
        missing = 'src/' + TOKEN_A + '.py'
        present = 'src/' + TOKEN_B + '.py'
        with open(os.path.join(self.repo, present), 'w', encoding='utf-8') as fh:
            fh.write('X = 1\n')
        b = self._build((missing, present))
        facts = b.state['facts']
        self.assertEqual(facts['files_named'], [MARK, MARK])
        self.assertEqual(facts['files_exist'], {MARK: True})
        dump = _canon(b.state)
        self.assertNotIn(TOKEN_A, dump)
        self.assertNotIn(TOKEN_B, dump)


if __name__ == '__main__':
    unittest.main()
