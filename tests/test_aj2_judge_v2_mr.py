'''AJ2 locked tests: question set route.v2 and expand_v2, state mr.route.v2, the strict v2 reader
(parse_answers_v2) and read_route_v2 / to_shadow for the files and tag-evidence shapes. No claude, no network.'''
import importlib
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest.mock import patch

from mr.facts import Candidate, Facts, RiskItem
from mr.task import Params, Task

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JUDGE_DIR = os.path.join(ROOT, 'mr', 'judge')
NL = chr(10)
TEXT = 'Fix the login token check in src/app.py so expired sessions sign out'
# Fake secret built by concatenation so gitleaks and the public-tree gate never see a literal.
TOKEN = 'sk' + '-' + 'Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56Qr78St90'
MARK = 'src/<redacted:token>.py'
HINT = 'src/caller_hint_zq.py'
PARAM = 'zqplantedparam'
RULES = {'type': 'code', 'difficulty': 'medium', 'risk': 'high'}
TYPE_FEATURES = {'lead_verb': 'fix', 'test_only_scope': False, 'keyword_hits': {'code': 1}}
SMALL_Q = {'q': {'type': 'noul', 'instructions': 'x'}}
FIXED = ('task_type', 'difficulty', 'risk', 'route_class', 'needs_clarification', 'load_bearing', 'high_judgment',
         'needs_review', 'ambiguity', 'novelty', 'files.change', 'files.new', 'files.complete')
APP = Candidate('src/app.py', True, ('named', 'exists'))
UTIL = Candidate('src/util.py', True, ('symbol:check_token',))
NEW = Candidate('src/new_mod.py', False, ('named', 'new'))
AUTH = RiskItem('auth', 'text', 'login token', None)
EXIST = {'src/app.py': True, 'src/new_mod.py': False, 'README.md': True}


def make_facts(cands=(APP, UTIL, NEW), risk=(AUTH,), paths_exist=None, tokens=('src/app.py', 'src/new_mod.py')):
    return Facts(path_tokens=tuple(tokens), paths_exist=dict(EXIST if paths_exist is None else paths_exist),
                 candidates=tuple(cands), risk_evidence=tuple(risk), sensitive_paths=(),
                 type_features=dict(TYPE_FEATURES), repo_files=12)


def many(n):
    return tuple(Candidate('src/m%02d.py' % i, True, ('glob',)) for i in range(n))


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from all_keys(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from all_keys(v)


def default_answer(qid, q):
    if qid == 'files.change':
        return {'answer': ['src/app.py'], 'confidence': 0.9}
    if qid == 'files.new':
        return {'answer': ['src/brand_new.py'], 'confidence': 0.8}
    if qid.startswith('tag.'):
        return {'answer': True, 'confidence': 0.9, 'evidence': 'login token'}
    kind = q.get('type')
    if kind == 'choice':
        return {'answer': list(q['criteria'])[0], 'confidence': 0.9}
    if kind == 'score':
        return {'answer': 0, 'confidence': 0.9}
    return {'answer': True, 'confidence': 0.9}


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.repo = os.path.join(self.root, 'repo')
        os.makedirs(os.path.join(self.repo, 'src'))
        home = os.path.join(self.root, 'home')
        os.makedirs(home)
        env = patch.dict(os.environ, {'MR_HOME': home})
        env.start()
        self.addCleanup(env.stop)

    def fn(self, modname, name):
        try:
            mod = importlib.import_module(modname)
        except ModuleNotFoundError as exc:
            self.fail(f'{modname} is missing: {exc}')
        fn = getattr(mod, name, None)
        self.assertTrue(callable(fn), f'{modname}.{name} is missing')
        return fn

    def expand(self, facts):
        expand_v2 = self.fn('mr.judge.questions', 'expand_v2')
        base = importlib.import_module('mr.judge.questions').load('route.v2')
        return expand_v2(base, facts)

    def sha(self, questions):
        return importlib.import_module('mr.judge.questions').qset_sha(questions)


class QuestionsV2Test(_Base):
    def test_fixed_questions_and_task_type_wording(self):
        q = self.expand(make_facts(risk=()))
        self.assertEqual([k for k in FIXED if k not in q], [])
        text = q['task_type']['instructions']
        self.assertIn('Name the deliverable.', text)
        self.assertIn('Mentions of tests as constraints do not make the task a test task.', text)
        self.assertIn('Are all files the change needs in the candidate list?', q['files.complete']['instructions'])
        self.assertEqual([k for k in q if k.startswith('tag.')], [], 'v2 has no static tag questions')

    def test_tags_only_from_text_items_one_per_tag(self):
        risk = (AUTH, RiskItem('auth', 'text', 'sessions', None),
                RiskItem('payments', 'path', None, 'src/pay/stripe.ts'), RiskItem('secrets', 'text', 'api key', None))
        q = self.expand(make_facts(risk=risk))
        self.assertEqual(sorted(k for k in q if k.startswith('tag.')), ['tag.auth', 'tag.secrets'])
        self.assertIn('auth', q['tag.auth']['instructions'])
        self.assertIn('Quote the words that decide it', q['tag.auth']['instructions'])

    def test_files_change_options_capped_and_sha_tracks_candidates(self):
        cands = many(35)
        q = self.expand(make_facts(cands=cands))
        self.assertEqual(q['files.change']['options'], [c.path for c in cands[:30]])
        self.assertEqual(self.sha(q), self.sha(self.expand(make_facts(cands=cands))))
        self.assertNotEqual(self.sha(q), self.sha(self.expand(make_facts(cands=cands[1:]))))
        base = importlib.import_module('mr.judge.questions').load('route.v2')
        self.assertNotEqual(self.sha(q), self.sha(base))


class StateV2Test(_Base):
    def build(self, facts, judge=None, task=None):
        fn = self.fn('mr.judge.state', 'build_route_state_v2')
        task = task or Task(id='t-0000a2b0', text=TEXT, repo=self.repo, base='abc1234', params=Params(),
                            files_named=(), source='arg')
        block = judge if judge is not None else {'stacks': {'demo': 'python stdlib, unittest'}}
        return fn(task, facts, types.SimpleNamespace(judge=block), 'demo', SMALL_Q, env={}, rules=dict(RULES))

    def test_exact_shape(self):
        risk = (AUTH, RiskItem('migration', 'path', None, 'db/001.sql'))
        b = self.build(make_facts(cands=(APP,) + many(34), risk=risk))
        s = json.loads(json.dumps(b.state))
        self.assertEqual(set(s), {'kind', 'task', 'repo', 'facts'})
        self.assertEqual((s['kind'], s['task']), ('mr.route.v2', TEXT))
        self.assertEqual(s['repo'], {'name': 'demo', 'stack': 'python stdlib, unittest', 'size_files': 12})
        f = s['facts']
        self.assertEqual(set(f), {'path_tokens', 'paths_exist', 'candidates', 'risk_candidates', 'sensitive_paths',
                                  'type_features', 'rules_estimate'})
        self.assertEqual(len(f['candidates']), 30)
        self.assertEqual(f['candidates'][0], {'path': 'src/app.py', 'why': ['named', 'exists']})
        self.assertEqual(f['risk_candidates'], [{'tag': 'auth', 'source': 'text', 'quote': 'login token'}])
        self.assertEqual(f['paths_exist'], EXIST)
        self.assertEqual(f['path_tokens'], ['src/app.py', 'src/new_mod.py'])
        self.assertEqual(f['type_features'], TYPE_FEATURES)
        self.assertEqual(f['rules_estimate'], RULES)
        self.assertEqual((b.status, b.egress), (None, 'excerpts'))

    def test_no_caller_hints_or_params(self):
        task = Task(id='t-0000a2b1', text=TEXT, repo=self.repo, base='abc1234',
                    params=Params(type='ios-native', prefer=PARAM), files_named=(HINT,), source='arg')
        b = self.build(make_facts(), task=task)
        dump = canon(b.state)
        for planted in (HINT, PARAM, 'ios-native'):
            self.assertNotIn(planted, dump)
        keys = set(all_keys(b.state))
        for banned in ('params', 'hints', 'files_named', 'caller'):
            self.assertNotIn(banned, keys)

    def test_token_in_candidate_path_is_redacted_everywhere(self):
        bad = 'src/' + TOKEN + '.py'
        facts = make_facts(cands=(Candidate(bad, False, ('named', 'new')), APP), risk=(),
                           paths_exist={bad: False, 'src/app.py': True}, tokens=(bad, 'src/app.py'))
        b = self.build(facts)
        self.assertNotIn(TOKEN, canon(b.state))
        f = b.state['facts']
        self.assertEqual(list(f['path_tokens'])[0], MARK)
        self.assertEqual(f['candidates'][0]['path'], MARK)
        self.assertEqual(f['paths_exist'], {MARK: False, 'src/app.py': True})
        self.assertGreaterEqual(b.redactions.get('token', 0), 3)
        self.assertIsNone(b.status)

    def test_too_long_and_redaction_abort(self):
        small = self.build(make_facts(), judge={'state_limits_tokens': {'route': 50}})
        self.assertEqual(small.status, 'too_long')
        mails = tuple('docs/u%d' % i + '@' + 'example.org.md' for i in range(13))
        facts = make_facts(cands=tuple(Candidate(m, True, ('glob',)) for m in mails), risk=(),
                           paths_exist={m: True for m in mails}, tokens=mails)
        b = self.build(facts)
        self.assertEqual(b.status, 'redaction_abort')
        self.assertNotIn('example.org', canon(b.state))

    def test_secrets_egress_needs_a_path_item(self):
        path_item = self.build(make_facts(risk=(RiskItem('secrets', 'path', None, '.env.local'),)))
        self.assertEqual(path_item.egress, 'metadata')
        text_only = self.build(make_facts(risk=(RiskItem('secrets', 'text', 'api key', None),)))
        self.assertEqual(text_only.egress, 'excerpts')


class ParseV2Test(_Base):
    def parse(self, facts=None, over=None, raw=None):
        render = self.fn('mr.judge.prompt', 'render_prompt_v2')
        parse = self.fn('mr.judge.prompt', 'parse_answers_v2')
        facts = facts or make_facts()
        questions = self.expand(facts)
        prompt, qmap = render({'kind': 'mr.route.v2'}, questions)
        key = {qid: k for k, qid in qmap.items()}
        over = over or {}
        given = {key[qid]: over.get(qid, default_answer(qid, q)) for qid, q in questions.items()}
        reply = raw(given) if raw else json.dumps({'answers': given})
        return parse(reply, qmap, questions, facts, TEXT), prompt

    def test_valid_answers_parse_to_compact_forms(self):
        parsed, prompt = self.parse()
        self.assertEqual((parsed.status, parsed.unsure), ('ok', ()))
        a = parsed.answers
        self.assertEqual(a['files.change'], {'picks': ['src/app.py'], 'conf': 0.9})
        self.assertEqual(a['files.new'], {'paths': ['src/brand_new.py'], 'conf': 0.8})
        self.assertEqual(a['files.complete'], {'p': 0.9})
        self.assertEqual(a['tag.auth'], {'p': 0.9, 'evidence': 'login token', 'quote_found': True})
        self.assertIn('src/app.py', prompt)
        for qid in ('files.change', 'files.new', 'tag.auth'):
            self.assertNotIn(qid, prompt)
        with open(os.path.join(JUDGE_DIR, 'judge.v2.txt'), encoding='utf-8') as fh:
            self.assertIn('evidence', fh.read())

    def test_change_picks_outside_options_dropped_and_over_12_unsure(self):
        pick = {'answer': ['src/app.py', 'src/elsewhere.py'], 'confidence': 0.9}
        parsed, _ = self.parse(over={'files.change': pick})
        self.assertEqual(parsed.status, 'ok')
        self.assertEqual(parsed.answers['files.change']['picks'], ['src/app.py'])
        facts = make_facts(cands=many(20))
        opts = [c.path for c in many(20)]
        parsed, _ = self.parse(facts=facts, over={'files.change': {'answer': opts[:12], 'confidence': 0.9}})
        self.assertEqual(parsed.answers['files.change']['picks'], opts[:12])
        parsed, _ = self.parse(facts=facts, over={'files.change': {'answer': opts[:13], 'confidence': 0.9}})
        self.assertNotIn('files.change', parsed.answers)
        self.assertIn('files.change', parsed.unsure)
        self.assertEqual(parsed.status, 'ok_partial')

    def test_new_paths_dropped_when_existing_or_outside_and_over_6_unsure(self):
        given = ['src/brand_new.py', '/etc/zq_abs.py', '../outside.py', 'src/app.py', 'README.md', 'src/new_mod.py']
        parsed, _ = self.parse(over={'files.new': {'answer': given, 'confidence': 0.8}})
        self.assertEqual(parsed.answers['files.new'], {'paths': ['src/brand_new.py', 'src/new_mod.py'], 'conf': 0.8})
        seven = ['src/n%d.py' % i for i in range(7)]
        parsed, _ = self.parse(over={'files.new': {'answer': seven, 'confidence': 0.8}})
        self.assertNotIn('files.new', parsed.answers)
        self.assertIn('files.new', parsed.unsure)

    def test_evidence_quote_found_is_case_and_space_insensitive(self):
        quote = 'LOGIN ' + NL + '  Token'
        parsed, _ = self.parse(over={'tag.auth': {'answer': True, 'confidence': 0.9, 'evidence': quote}})
        self.assertEqual(parsed.answers['tag.auth'], {'p': 0.9, 'evidence': quote, 'quote_found': True})
        parsed, _ = self.parse(over={'tag.auth': {'answer': False, 'confidence': 0.8, 'evidence': 'credit card'}})
        got = parsed.answers['tag.auth']
        self.assertEqual((got['evidence'], got['quote_found']), ('credit card', False))
        self.assertAlmostEqual(got['p'], 0.2)

    def test_strict_reply(self):
        parsed, _ = self.parse(raw=lambda g: json.dumps({'answers': g}) + ' ' + json.dumps({'answers': g}))
        self.assertEqual(parsed.status, 'invalid_response')
        parsed, _ = self.parse(raw=lambda g: json.dumps({'answers': g, 'note': 'x'}))
        self.assertEqual(parsed.status, 'invalid_response')
        parsed, _ = self.parse(raw=lambda g: json.dumps({'answers': dict(g, q99={'answer': True, 'confidence': 0.9})}))
        self.assertEqual(parsed.status, 'invalid_response')
        parsed, _ = self.parse(over={'tag.auth': {'answer': True, 'confidence': 0.9},
                                     'files.change': {'answer': ['src/app.py'], 'confidence': 0.9, 'why': 'x'}})
        self.assertEqual(parsed.status, 'ok_partial')
        self.assertEqual(sorted(parsed.unsure), ['files.change', 'tag.auth'])


class PolicyV2Test(_Base):
    def read(self, answers, unsure=()):
        read = self.fn('mr.judge.policy', 'read_route_v2')
        parsed_cls = importlib.import_module('mr.judge.prompt').ParsedAnswers
        parsed = parsed_cls('ok_partial' if unsure else 'ok', dict(answers), tuple(unsure))
        return parsed, read(parsed, {'risk': 'low', 'scope': 0}, types.SimpleNamespace(judge={}))

    def test_field_readings_at_v1_thresholds(self):
        _, d = self.read({'task_type': {'c': 'code', 'conf': 0.8}, 'route_class': {'c': 'design', 'conf': 0.7},
                          'risk': {'s': 2, 'conf': 0.65}, 'difficulty': {'s': 3, 'conf': 0.9},
                          'ambiguity': {'s': 1, 'conf': 0.55}, 'needs_review': {'p': 0.9}})
        self.assertEqual(d['task_type'], {'decision': 'act', 'value': 'code', 'conf': 0.8})
        self.assertEqual(d['route_class']['decision'], 'unsure')
        self.assertEqual((d['risk']['decision'], d['risk']['value']), ('raise', 2))
        self.assertAlmostEqual(d['risk']['conf'], 0.65)
        self.assertEqual((d['difficulty']['decision'], d['difficulty']['value']), ('act', 3))
        self.assertEqual(d['ambiguity']['decision'], 'unsure')
        self.assertEqual((d['needs_review']['decision'], d['needs_review']['value']), ('raise', True))

    def test_files_and_tags_readings(self):
        _, d = self.read({'files.change': {'picks': ['src/app.py'], 'conf': 0.9},
                          'files.new': {'paths': ['src/x.py'], 'conf': 0.8}, 'files.complete': {'p': 0.9},
                          'tag.auth': {'p': 0.9, 'evidence': 'login token', 'quote_found': True},
                          'tag.secrets': {'p': 0.2, 'evidence': 'api key', 'quote_found': False},
                          'tag.deps': {'p': 0.45, 'evidence': None, 'quote_found': False}})
        files = d['files']
        self.assertEqual((files['change'], files['new'], files['complete']), (['src/app.py'], ['src/x.py'], True))
        self.assertEqual(set(files['conf']), {'change', 'new', 'complete'})
        self.assertAlmostEqual(files['conf']['change'], 0.9)
        self.assertAlmostEqual(files['conf']['new'], 0.8)
        auth, sec = d['tags']['auth'], d['tags']['secrets']
        self.assertEqual((auth['changes_behavior'], auth['evidence'], auth['quote_found']), (True, 'login token', True))
        self.assertAlmostEqual(auth['conf'], 0.9)
        self.assertEqual((sec['changes_behavior'], sec['quote_found']), (False, False))
        self.assertAlmostEqual(sec['conf'], 0.8)
        self.assertIsNone(d['tags']['deps']['changes_behavior'])
        _, d = self.read({'files.complete': {'p': 0.6}}, unsure=('files.change', 'files.new', 'tag.auth'))
        files = d['files']
        self.assertEqual((files['change'], files['new'], files['complete']), ([], [], None))
        self.assertIsNone(files['conf']['change'])
        self.assertEqual(d['tags']['auth'], {'changes_behavior': None, 'evidence': None, 'quote_found': False,
                                             'conf': None})

    def test_to_shadow_maps_v2_readings(self):
        parsed, d = self.read({'route_class': {'c': 'design', 'conf': 0.9}, 'task_type': {'c': 'code', 'conf': 0.9},
                               'difficulty': {'s': 3, 'conf': 0.9}, 'risk': {'s': 2, 'conf': 0.9},
                               'files.change': {'picks': ['src/app.py'], 'conf': 0.9},
                               'tag.auth': {'p': 0.9, 'evidence': 'login token', 'quote_found': True},
                               'tag.secrets': {'p': 0.2, 'evidence': 'api key', 'quote_found': False}})
        policy = importlib.import_module('mr.judge.policy')
        ans = policy.to_shadow(parsed, d, ref='j-t-0000a2b2-1', status='ok', consulted=True, latency_ms=5)
        got = (ans.route, ans.type, ans.difficulty, ans.risk, tuple(ans.risk_tags), ans.status)
        self.assertEqual(got, ('design', 'code', 'hard', 'high', ('auth',), 'ok'))


if __name__ == '__main__':
    unittest.main()
