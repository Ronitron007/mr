'''AJ1 locked tests: the facts stage is logged only. Route record `facts` counts and redacted `risk_evidence`
in `mr run`, a facts failure never changes the run, the v2 log schema learns the keys, and `mr route` shows
facts in JSON and text (also with --no-models) while every decision stays as before. Fake runners only.'''
import contextlib
import dataclasses
import importlib
import io
import json
import os
import re
import unittest
from unittest.mock import patch

from mr import cli, prepass, router
from mr.task import Params, make_task
from tests import gitfix, log_schema_v2
from tests.test_run_once import RunCase

FACT_KEYS = {'path_tokens', 'exists', 'new', 'candidates', 'by_why', 'sensitive', 'repo_files'}
TOKEN = 'sk-' + 'abcdefghijklmnop' + '1234567890'
README = {'files': {'README.md': '# test repo\nfixed typo\n'}}
LINE = re.compile(r'^facts: (\d+) candidates \((\d+) exist, (\d+) new\), sensitive: (\d+)$', re.M)
ROUTE_REC = {'ts': '2026-11-02T12:00:00+05:30', 'v': 2, 'task': 't-fixture', 'mr_version': '0.1.0',
             'role': 'route', 'fingerprint': None, 'rules': {'route': 'cascade'},
             'applied': {'route': 'cascade', 'ladder': ['G-x']}, 'sources': {'route': 'default'},
             'invariant_violation': False, 'judge': None, 'classifier': None}
TEXTS = (
    'fix typo in README.md', 'implement a new endpoint in src/api/users.ts',
    'refactor src/store/cart.ts and src/store/user.ts', 'write tests for src/util.py',
    'fix the Authentication required error message', 'add supabase/migrations/0042_x.sql for orders',
    'update .env.local with the new key', 'explain how the router works', 'review the payments flow',
    'delete old records in the purge job', 'bump the lockfile and package.json', 'maybe improve something?',
    'document the CLI in docs/usage.md', 'rename load_store to read_store in src/app/store.py',
    'add dark mode to the settings screen', 'plan the migration roadmap', 'fix crash in ios/Podfile build',
    'update `shared_helper` usage everywhere', 'Implement y', 'Write tests for x',
)


def _facts_mod():
    return importlib.import_module('mr.facts')


def _plain(obj):
    if isinstance(obj, (set, frozenset)):
        return sorted(_plain(v) for v in obj)
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


class RunFactsTest(RunCase):
    def setUp(self):
        super().setUp()
        self.env['MR_HOME'] = self.home

    def route_rec(self):
        recs = self.records('route')
        self.assertEqual(len(recs), 1, recs)
        return recs[0]

    def test_run_route_record_has_facts_counts(self):
        self.cursor(README)
        self.env['MR_HOME'] = self.home
        code, _out, err = self.mr()
        self.assertEqual(code, 0, err)
        rec = self.route_rec()
        facts = rec.get('facts')
        self.assertIsInstance(facts, dict, rec)
        self.assertEqual(set(facts), FACT_KEYS)
        self.assertEqual({k: facts[k] for k in FACT_KEYS - {'by_why'}},
                         {'path_tokens': 1, 'exists': 1, 'new': 0, 'candidates': 1, 'sensitive': 0,
                          'repo_files': 2})
        self.assertEqual(facts['by_why'].get('named'), 1, facts)
        self.assertEqual(rec.get('risk_evidence'), [])
        self.assert_schema()

    def test_run_risk_evidence_redacted_no_raw_token(self):
        text = 'fix typo in README.md\nkeep the secret ' + TOKEN + ' out of the password docs'
        self.mr(text=text)
        rec = self.route_rec()
        ev = rec.get('risk_evidence')
        self.assertIsInstance(ev, list, rec)
        self.assertIn({'tag': 'secrets', 'source': 'text', 'quote': 'secret'}, ev)
        self.assertIn({'tag': 'auth', 'source': 'text', 'quote': 'password'}, ev)
        for item in ev:
            self.assertIn(set(item), ({'tag', 'source', 'quote'}, {'tag', 'source', 'path'}), item)
        with open(os.path.join(self.home, 'log', 'runs.jsonl'), encoding='utf-8') as fh:
            self.assertNotIn(TOKEN, fh.read())
        self.assert_schema()

    def test_run_facts_error_logged_run_unchanged(self):
        mod = _facts_mod()
        self.cursor(README)
        self.env['MR_HOME'] = self.home
        with patch.object(mod, 'build_facts', side_effect=RuntimeError('boom')):
            code, _out, err = self.mr()
        self.assertEqual(code, 0, err)
        rec = self.route_rec()
        self.assertEqual(rec.get('facts'), {'error': 'RuntimeError'})
        self.assertEqual(rec.get('risk_evidence'), [])
        self.assertEqual(self.only_task()['outcome'], 'accepted')
        self.assert_schema()

    def test_schema_checks_new_optional_keys(self):
        good = dict(ROUTE_REC, facts={'path_tokens': 1, 'exists': 1, 'new': 0, 'candidates': 1, 'by_why': {},
                                      'sensitive': 0, 'repo_files': 2},
                    risk_evidence=[{'tag': 'auth', 'source': 'text', 'quote': 'login'}])
        self.assertEqual(log_schema_v2.check(good), [])
        self.assertEqual(log_schema_v2.check(dict(ROUTE_REC, facts={'error': 'OSError'}, risk_evidence=[])), [])
        self.assertNotEqual(log_schema_v2.check(dict(good, facts='3 candidates')), [])
        self.assertNotEqual(log_schema_v2.check(dict(good, risk_evidence='auth')), [])
        self.assertNotEqual(log_schema_v2.check(dict(good, risk_evidence=['auth'])), [])


class RouteFactsTest(RunCase):
    repo_files = {'src/auth/login.ts': 'export const login = 1;\n'}
    TEXT = 'update src/auth/login.ts and add src/auth/new_flow.ts'

    def setUp(self):
        super().setUp()
        self.env['MR_HOME'] = self.home

    def route(self, text, json_out=False):
        argv = (['--config', self.cfgpath] + (['--json'] if json_out else [])
                + ['route', text, '--repo', self.repo, '--no-models'])
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_route_json_has_facts_and_risk_evidence(self):
        _code, out, err = self.route(self.TEXT, json_out=True)
        doc = json.loads(out)
        facts = doc.get('facts')
        self.assertIsInstance(facts, dict, doc)
        self.assertEqual(set(facts), FACT_KEYS)
        self.assertEqual({k: facts[k] for k in FACT_KEYS - {'by_why'}},
                         {'path_tokens': 2, 'exists': 1, 'new': 1, 'candidates': 2, 'sensitive': 2,
                          'repo_files': 3})
        self.assertEqual(facts['by_why'].get('named'), 2, facts)
        ev = doc.get('risk_evidence')
        self.assertIsInstance(ev, list, doc)
        self.assertIn({'tag': 'auth', 'source': 'path', 'path': 'src/auth/login.ts'}, ev)
        self.assertIn({'tag': 'auth', 'source': 'path', 'path': 'src/auth/new_flow.ts'}, ev)

    def test_route_human_line_no_models(self):
        _code, out, err = self.route(self.TEXT)
        found = LINE.findall(out)
        self.assertEqual(found, [('2', '1', '1', '2')], out)
        self.assertIn('route: ', out)

    def test_route_decisions_identical_for_20_texts(self):
        self.assertEqual(len(TEXTS), 20)
        cfg = self.cfg()
        base = gitfix.head(self.repo)
        for text in TEXTS:
            _code, out, err = self.route(text, json_out=True)
            doc = json.loads(out)
            self.assertIn('facts', doc, text)
            self.assertIsInstance(doc.get('risk_evidence'), list, text)
            task = make_task(text, self.repo, base, Params())
            feats = prepass.extract(task, None)
            want = _plain(dataclasses.asdict(router.decide(task, feats, cfg)))
            self.assertEqual(doc['decision'], json.loads(json.dumps(want)), text)
            self.assertEqual(doc['features'], json.loads(json.dumps(_plain(dataclasses.asdict(feats)))), text)


if __name__ == '__main__':
    unittest.main()
