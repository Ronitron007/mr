'''SHD5 locked tests: mr.judge route state (spec 9.7), shadow policy reading (spec 9.5) and ShadowAnswer mapping.'''
import dataclasses
import hashlib
import importlib
import json
import math
import os
import shutil
import tempfile
import types
import unittest
from unittest.mock import patch

from mr.prepass import extract_from
from mr.shadow import ShadowAnswer
from mr.task import Params, Task

TEXT = 'Fix the typo in README.md heading'
FACT_KEYS = {'files_named', 'files_exist', 'grep_hit_files', 'risk_tags_rules', 'risk_floor', 'difficulty_floor',
             'cross_layer', 'public_interface', 'new_module', 'ambiguity_heuristic', 'type_keyword_hits'}
RULES = {'route': 'cascade', 'type': 'bugfix', 'difficulty': 'easy', 'risk': 'low', 'risk_tags': []}
SIX = (0.249, 0.25, 0.599, 0.60, 0.749, 0.75)
LONG = 'lorem ipsum ' * 450


def _same(value):
    return (value, value)


# Fake secrets are split so the public-tree gate and gitleaks never see a key-like literal.
X36 = 'A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8'
PEM_BODY = 'MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX'
TOKENS = (
    'sk' + '-' + 'Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56Qr78St90',
    'sk' + '-or-v1-' + '0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b',
    'gh' + 'p_' + X36,
    'gh' + 'o_' + X36,
    'gh' + 's_' + X36,
    'gh' + 'u_' + X36,
    'github' + '_pat_' + '11AbCdEfGh_IjKlMnOpQrStUvWxYz012345',
    'xox' + 'b-' + 'Ab12-Cd34-Ef56Gh78',
    'AK' + 'IA' + 'IOSFODNN7EXAMPLE',
    'AI' + 'za' + ('Ab3-' * 9)[:35],
    'sb' + 'p_' + 'a1b2c3d4e5' * 4,
    'sk' + '_live_' + 'Ab12Cd34Ef56Gh78Ij90',
    'rk' + '_test_' + 'Zy98Xw76Vu54Ts32Rq10',
    'pk' + '_live_' + 'Mn34Op56Qr78St90Uv12',
)
ENV = {
    'MY_API_TOKEN': 'plainvalue-xyz-01',  # gitleaks:allow (fake test value)
    'DB_PASSWORD': 'envpass-zz-9876',  # gitleaks:allow (fake test value)
    'STRIPE_SECRET': 'stripe-env-val-55',  # gitleaks:allow (fake test value)
}
CORPUS = [_same(t) for t in TOKENS] + [
    ('-----BEGIN ' + 'RSA PRIVATE KEY-----\n' + PEM_BODY + '\n-----END ' + 'RSA PRIVATE KEY-----', PEM_BODY),
    _same('eyJ' + 'hbGciOiJIUzI1NiJ9' + '.' + 'eyJzdWIiOiJ1c2VyLTEifQ' + '.' + 'c2lnbmF0dXJlLXZhbHVlLWhlcmU'),
    ('PASS' + 'WORD=' + 'hunter2hunter2', 'hunter2hunter2'),
    ('api' + '_key: ' + 'Qw7Er8Ty9Ui0Op', 'Qw7Er8Ty9Ui0Op'),
    ('client' + '_secret=' + 'Zx9Yw8Vu7Ts6', 'Zx9Yw8Vu7Ts6'),
    ('https://' + 'bob:s3cretpw' + '@' + 'git.example.com/repo.git', 's3cretpw'),
    _same('aB3dE5gH7jK9mN1pQ2rS4tU6vW8xY0zCfLiO'),
    _same('Zq8Wv7Ut6Sr5Qp4On3Ml2Kj1Ih0GfEdCbAz9y'),
    _same('ops' + '@' + 'example.org'),
    _same('dev' + '@' + 'example.net'),
    _same('qa' + '@' + 'example.com'),
    ('call 5551234567', '5551234567'),
    ('call +919876543210', '919876543210'),
] + [_same(v) for v in ENV.values()]


def _mod(case, name):
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        case.fail(f'{name} is missing: {exc}')


def _cfg(**judge):
    return types.SimpleNamespace(judge=judge or None)


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


class _Base(unittest.TestCase):
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

    def _build(self, text=TEXT, files=('README.md',), cfg=None, params=None, env=None, features=None):
        st = _mod(self, 'mr.judge.state')
        qs = _mod(self, 'mr.judge.questions').load()
        task = Task(id='t-0000abcd', text=text, repo=self.repo, base='abc1234', params=params or Params(),
                    files_named=tuple(files), source='arg')
        feat = features if features is not None else extract_from(text, files)
        return st.build_route_state(task, feat, cfg or _cfg(), 'demo', qs, env=env or {})


class RouteStateTest(_Base):
    def test_route_state_shape_without_caller_params(self):
        params = Params(type='ios-native', difficulty='critical', route='design', prefer='zz-prefer-mark')
        b = self._build(params=params)
        self.assertIsNone(b.status)
        s = b.state
        self.assertEqual(set(s), {'kind', 'task', 'repo', 'stack', 'facts', 'paths_sample'})
        self.assertEqual((s['kind'], s['task'], s['repo'], s['stack'], s['paths_sample']),
                         ('mr.route.v1', TEXT, 'demo', None, []))
        facts = s['facts']
        self.assertEqual(set(facts), FACT_KEYS)
        feat = extract_from(TEXT, ('README.md',))
        self.assertEqual(facts['files_named'], ['README.md'])
        self.assertEqual(facts['files_exist'], {'README.md': True})
        self.assertEqual(facts['type_keyword_hits'], {'bugfix': 1})
        self.assertEqual(facts['risk_tags_rules'], [])
        self.assertEqual([facts['risk_floor'], facts['difficulty_floor'], facts['cross_layer'],
                          facts['public_interface'], facts['new_module'], facts['ambiguity_heuristic'],
                          facts['grep_hit_files']],
                         [feat.risk_floor, feat.difficulty_floor, feat.cross_layer, feat.public_interface,
                          feat.new_module, feat.ambiguity, feat.grep_hit_files])
        dump = json.dumps(s)
        for leak in ('ios-native', 'critical', 'design', 'zz-prefer-mark', 't-0000abcd', 'abc1234'):
            self.assertNotIn(leak, dump)

    def test_state_sha_bytes_and_token_estimate(self):
        st = _mod(self, 'mr.judge.state')
        qs = _mod(self, 'mr.judge.questions').load()
        names = {'state', 'state_sha', 'state_bytes', 'est_tokens', 'redactions', 'truncated', 'egress', 'status'}
        self.assertEqual({f.name for f in dataclasses.fields(st.BuiltState)}, names)
        b = self._build()
        raw = _canon(b.state)
        self.assertEqual(b.state_sha, hashlib.sha256(raw).hexdigest()[:16])
        self.assertEqual(b.state_bytes, len(raw))
        self.assertEqual(b.est_tokens, math.ceil(len(raw) / 3.0) + math.ceil(len(_canon(qs)) / 3.0))
        self.assertEqual((b.redactions, b.truncated, b.egress), ({}, False, 'excerpts'))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            b.status = 'too_long'

    def test_stack_from_config_and_caps(self):
        b = self._build(cfg=_cfg(stacks={'demo': 'Python CLI; stdlib only'}))
        self.assertEqual(b.state['stack'], 'Python CLI; stdlib only')
        self.assertIsNone(self._build(cfg=_cfg(stacks={'other': 'Go'})).state['stack'])
        files = tuple(f'src/m{i}.py' for i in range(40))
        big = self._build(text=LONG, files=files)
        self.assertEqual(big.state['task'], LONG[:4000])
        self.assertEqual(big.state['facts']['files_named'], list(files[:30]))
        self.assertEqual(set(big.state['facts']['files_exist']), set(files[:30]))

    def test_files_exist_never_true_outside_repo(self):
        outside = os.path.join(self.root, 'x')
        with open(outside, 'w', encoding='utf-8') as fh:
            fh.write('outside\n')
        with open(os.path.join(self.repo, 'src', 'a.py'), 'w', encoding='utf-8') as fh:
            fh.write('A = 1\n')
        os.symlink(self.root, os.path.join(self.repo, 'out'))
        names = ('src/a.py', 'missing.py', '../x', 'out/x', 'src/../../x', outside)
        fe = self._build(text='Fix src/a.py', files=names).state['facts']['files_exist']
        self.assertIs(fe['src/a.py'], True)
        self.assertEqual([k for k, v in fe.items() if v], ['src/a.py'])
        self.assertTrue(all(isinstance(v, bool) for v in fe.values()))

    def test_files_exist_keys_are_redacted_and_counted(self):
        token = TOKENS[0]
        mail = 'qa.lead' + '@' + 'example.net'
        files = ('src/' + token + '.py', 'docs/' + mail + '.md', 'README.md')
        b = self._build(files=files)
        dump = _canon(b.state).decode('utf-8')
        self.assertNotIn(token, dump)
        self.assertNotIn(mail, dump)
        facts = b.state['facts']
        self.assertEqual(list(facts['files_exist']), facts['files_named'])
        self.assertIs(facts['files_exist']['README.md'], True)
        self.assertEqual(b.redactions, {'token': 2, 'email': 2})

    def test_redaction_corpus_absent_from_prompt(self):
        p = _mod(self, 'mr.judge.prompt')
        qs = _mod(self, 'mr.judge.questions').load()
        self.assertGreaterEqual(len(CORPUS), 30)
        for i in range(3):
            batch = CORPUS[i::3]
            text = 'Fix README.md.\n' + '\n'.join(frag for frag, _ in batch)
            b = self._build(text=text, env=ENV)
            self.assertIsNone(b.status, f'batch {i}')
            self.assertGreaterEqual(sum(b.redactions.values()), len(batch))
            prompt, _ = p.render_prompt(b.state, qs)
            for j, (_, needle) in enumerate(batch):
                self.assertFalse(needle in prompt, f'batch {i} item {j} leaked')

    def test_more_than_25_redactions_aborts(self):
        text = 'Fix README.md.\n' + '\n'.join(frag for frag, _ in CORPUS)
        b = self._build(text=text, env=ENV)
        self.assertEqual(b.status, 'redaction_abort')
        self.assertGreater(sum(b.redactions.values()), 25)

    def test_too_long_over_route_limit(self):
        qs = _mod(self, 'mr.judge.questions').load()
        limit = math.ceil(len(_canon(qs)) / 3.0) + 300
        cfg = _cfg(state_limits_tokens={'route': limit})
        small = self._build(cfg=cfg)
        self.assertIsNone(small.status)
        self.assertLessEqual(small.est_tokens, limit)
        files = tuple(f'src/deep/module_{i:02d}/handler.py' for i in range(30))
        big = self._build(text=LONG, files=files, cfg=cfg)
        self.assertEqual(big.status, 'too_long')
        self.assertGreater(big.est_tokens, limit)
        self.assertIsNone(self._build(text=LONG, files=files).status)
        self.assertEqual(self._build(cfg=_cfg(token_estimate_bytes_per_token=0.25)).status, 'too_long')

    def test_secrets_tag_forces_metadata_egress(self):
        feat = dataclasses.replace(extract_from(TEXT, ('README.md',)), risk_tags=frozenset({'secrets', 'auth'}))
        b = self._build(features=feat, cfg=_cfg(egress='excerpts'))
        self.assertEqual(b.egress, 'metadata')
        self.assertEqual(b.state['facts']['risk_tags_rules'], ['auth', 'secrets'])
        self.assertEqual(self._build().egress, 'excerpts')
        self.assertEqual(self._build(cfg=_cfg(egress='metadata')).egress, 'metadata')


class PolicyTest(_Base):
    def _read(self, answers, unsure=(), cfg=None, rules=RULES):
        pol = _mod(self, 'mr.judge.policy')
        pr = _mod(self, 'mr.judge.prompt')
        parsed = pr.ParsedAnswers(status='ok_partial' if unsure else 'ok', answers=answers, unsure=tuple(unsure))
        return pol.read_route(parsed, rules, cfg or _cfg())

    def _dec(self, qid, compact, **kw):
        return self._read({qid: compact}, **kw)[qid]['decision']

    def test_noul_soft_boundaries(self):
        got = [self._dec('needs_clarification', {'p': p}) for p in SIX]
        self.assertEqual(got, ['act', 'act', 'unsure', 'unsure', 'unsure', 'act'])
        self.assertIs(self._read({'needs_clarification': {'p': 0.25}})['needs_clarification']['value'], False)
        self.assertIs(self._read({'load_bearing': {'p': 0.75}})['load_bearing']['value'], True)
        self.assertEqual(self._dec('high_judgment', {'p': 0.749}), 'unsure')

    def test_noul_raise_only_boundaries(self):
        got = [self._dec('needs_review', {'p': p}) for p in SIX]
        self.assertEqual(got, ['ignored', 'ignored', 'unsure', 'raise', 'raise', 'raise'])
        self.assertEqual([self._dec('tag.auth', {'p': p}) for p in (0.599, 0.60)], ['unsure', 'raise'])
        self.assertEqual(self._dec('needs_worktree', {'p': 0.60}), 'raise')
        self.assertIs(self._read({'needs_review': {'p': 0.6}})['needs_review']['value'], True)

    def test_choice_boundaries(self):
        got = [self._dec('route_class', {'c': 'judgment', 'conf': c}) for c in SIX]
        self.assertEqual(got, ['unsure'] * 5 + ['act'])
        self.assertEqual(self._read({'route_class': {'c': 'judgment', 'conf': 0.75}})['route_class'],
                         {'decision': 'act', 'value': 'judgment'})

    def test_score_soft_boundaries(self):
        got = [self._dec('difficulty', {'s': 3, 'conf': c}) for c in SIX]
        self.assertEqual(got, ['unsure'] * 5 + ['act'])
        self.assertEqual(self._read({'difficulty': {'s': 3, 'conf': 0.75}})['difficulty'],
                         {'decision': 'act', 'value': 3})

    def test_raise_only_risk_acts_upward_at_raise_p(self):
        got = [self._dec('risk', {'s': 2, 'conf': c}) for c in SIX]
        self.assertEqual(got, ['unsure', 'unsure', 'unsure', 'raise', 'raise', 'raise'])
        self.assertEqual(self._read({'risk': {'s': 2, 'conf': 0.6}})['risk'], {'decision': 'raise', 'value': 2})
        high = dict(RULES, risk='high')
        medium = dict(RULES, risk='medium')
        self.assertEqual(self._dec('risk', {'s': 1, 'conf': 0.95}, rules=high), 'ignored')
        self.assertEqual(self._dec('risk', {'s': 1, 'conf': 0.95}, rules=medium), 'ignored')
        self.assertEqual(self._dec('risk', {'s': 2, 'conf': 0.95}, rules=medium), 'raise')
        self.assertEqual(self._dec('scope', {'s': 3, 'conf': 0.6}), 'raise')

    def test_abstain_band_is_unsure(self):
        self.assertEqual(self._dec('task_type', {'c': 'ui', 'conf': 0.5}), 'unsure')
        self.assertEqual(self._dec('task_type', {'c': 'ui', 'conf': 0.7}), 'unsure')
        self.assertEqual(self._dec('ambiguity', {'s': 2, 'conf': 0.74}), 'unsure')
        self.assertEqual([self._dec('high_judgment', {'p': p}) for p in (0.3, 0.5, 0.7)], ['unsure'] * 3)
        out = self._read({}, unsure=('difficulty', 'tag.auth'))
        self.assertEqual(out['difficulty'], {'decision': 'unsure', 'value': None})
        self.assertEqual(out['tag.auth'], {'decision': 'unsure', 'value': None})

    def test_novelty_always_ignored(self):
        self.assertEqual(self._dec('novelty', {'c': 'novel', 'conf': 0.99}), 'ignored')
        self.assertEqual(self._read({}, unsure=('novelty',))['novelty']['decision'], 'ignored')
        self.assertEqual(self._dec('start_rung', {'c': 'G-m', 'conf': 0.99}), 'ignored')

    def test_thresholds_read_from_config(self):
        cfg = _cfg(thresholds={'act_p': 0.9, 'raise_p': 0.7})
        self.assertEqual(self._dec('route_class', {'c': 'design', 'conf': 0.8}, cfg=cfg), 'unsure')
        self.assertEqual(self._dec('route_class', {'c': 'design', 'conf': 0.9}, cfg=cfg), 'act')
        self.assertEqual(self._dec('needs_review', {'p': 0.65}, cfg=cfg), 'unsure')
        self.assertEqual(self._dec('needs_review', {'p': 0.7}, cfg=cfg), 'raise')


class ToShadowTest(_Base):
    def test_to_shadow_maps_acted_and_raised(self):
        pol = _mod(self, 'mr.judge.policy')
        pr = _mod(self, 'mr.judge.prompt')
        answers = {
            'route_class': {'c': 'judgment', 'conf': 0.9}, 'task_type': {'c': 'ui', 'conf': 0.8},
            'difficulty': {'s': 3, 'conf': 0.8}, 'risk': {'s': 2, 'conf': 0.65},
            'tag.auth': {'p': 0.9}, 'tag.data_deletion': {'p': 0.7}, 'tag.native_config': {'p': 0.62},
            'tag.deps': {'p': 0.3}, 'tag.payments': {'p': 0.5}, 'novelty': {'c': 'novel', 'conf': 0.95},
        }
        parsed = pr.ParsedAnswers(status='ok_partial', answers=answers, unsure=('ambiguity',))
        decisions = pol.read_route(parsed, RULES, _cfg())
        sa = pol.to_shadow(parsed, decisions, ref='j-t-0000abcd-1', status='ok_partial', consulted=True,
                           latency_ms=420)
        self.assertIsInstance(sa, ShadowAnswer)
        self.assertEqual((sa.source, sa.mode, sa.status, sa.ref, sa.consulted, sa.latency_ms),
                         ('judge', 'shadow', 'ok_partial', 'j-t-0000abcd-1', True, 420))
        self.assertEqual((sa.route, sa.type, sa.difficulty, sa.risk), ('judgment', 'ui', 'hard', 'high'))
        self.assertEqual(tuple(sa.risk_tags), ('auth', 'data-deletion', 'native-config'))
        self.assertEqual(tuple(sa.unsure_fields), ('ambiguity', 'tag.payments'))
        self.assertIsNone(sa.first_rung)
        self.assertIsNone(sa.confidence)

    def test_to_shadow_nothing_acted_or_invalid(self):
        pol = _mod(self, 'mr.judge.policy')
        pr = _mod(self, 'mr.judge.prompt')
        answers = {'route_class': {'c': 'design', 'conf': 0.6}, 'task_type': {'c': 'docs', 'conf': 0.7},
                   'difficulty': {'s': 4, 'conf': 0.74}, 'risk': {'s': 0, 'conf': 0.9}, 'tag.secrets': {'p': 0.2}}
        parsed = pr.ParsedAnswers(status='ok', answers=answers, unsure=())
        sa = pol.to_shadow(parsed, pol.read_route(parsed, RULES, _cfg()), ref=None, status='ok', consulted=False,
                           latency_ms=None)
        self.assertEqual((sa.route, sa.type, sa.difficulty, sa.risk, tuple(sa.risk_tags)),
                         (None, None, None, None, ()))
        self.assertEqual(tuple(sa.unsure_fields), ('difficulty', 'route_class', 'task_type'))
        self.assertEqual((sa.source, sa.status, sa.consulted), ('judge', 'ok', False))
        bad = pr.ParsedAnswers(status='invalid_response', answers={}, unsure=())
        sb = pol.to_shadow(bad, pol.read_route(bad, RULES, _cfg()), ref='j-t-0000abcd-2',
                           status='invalid_response', consulted=True, latency_ms=50)
        self.assertEqual((sb.status, sb.route, sb.risk, tuple(sb.risk_tags), tuple(sb.unsure_fields)),
                         ('invalid_response', None, None, (), ()))


if __name__ == '__main__':
    unittest.main()
