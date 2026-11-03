'''AJ3 locked tests: mr.judge.merge, the pure policy merge for the always-on judge (floors > judge > hints > rules;
stricter/neutral/looser bars, pins, overrides, fallback, invariants I1'-I4'). No claude, git or network.'''
import dataclasses
import importlib
import os
import random
import shutil
import tempfile
import unittest
from unittest.mock import patch

from mr import router
from mr.config import from_dict
from mr.defaults import defaults
from mr.facts import Candidate, Facts, RiskItem
from mr.task import Params, Task

DIFF = ('trivial', 'easy', 'medium', 'hard', 'critical')
RISK = ('low', 'medium', 'high')
TYPES = ('code', 'bugfix', 'refactor', 'test', 'ios-native', 'ui', 'review', 'plan', 'explain', 'docs')
HIGH = frozenset({'auth', 'payments', 'migration', 'data-deletion'})
MED = frozenset({'native-config', 'deps', 'secrets'})
TAGS = tuple(sorted(HIGH | MED))
TEXT = 'Fix the login token check in src/app.py: one small file, docs only wording'
Q = 'one small file'
APP = Candidate('src/app.py', True, ('named', 'exists'))
CALLER = Candidate('src/caller.py', True, ('hint', 'exists'))
UTIL = Candidate('src/util.py', True, ('symbol:check_token',))
NEW = Candidate('src/new_mod.py', False, ('named', 'new'))
AUTH_TEXT = RiskItem('auth', 'text', 'login token', None)
AUTH_PATH = RiskItem('auth', 'path', None, 'src/auth/session.py')
DEPS_PATH = RiskItem('deps', 'path', None, 'package.json')


def many_named(n):
    return tuple(Candidate('src/m%02d.py' % i, True, ('named', 'exists')) for i in range(n))


def make_facts(cands=(APP,), risk=()):
    return Facts(path_tokens=tuple(c.path for c in cands if 'named' in c.why),
                 paths_exist={c.path: c.exists for c in cands}, candidates=tuple(cands),
                 risk_evidence=tuple(risk), sensitive_paths=tuple(i.path for i in risk if i.source == 'path'),
                 type_features={'lead_verb': 'fix', 'test_only_scope': False, 'keyword_hits': {}}, repo_files=40)


def rules(**kw):
    base = dict(route='cascade', type='code', difficulty='medium', risk='low', risk_tags=frozenset(),
                load_bearing=False, high_judgment=False, needs_review=True, isolate_hint=None,
                needs_clarification=False, ladder=('G-h', 'G-x', 'S'), reasons=(),
                sources={'route': 'default', 'type': 'rule', 'difficulty': 'rule', 'risk': 'rule'})
    base.update(kw)
    return router.Decision(**base)


def rd(value, conf, evidence=None, quote=False):
    '''A field reading as read_route_v2 gives it, plus the evidence and quote_found the looser bar needs.'''
    decision = 'act' if conf >= 0.75 else 'raise' if conf >= 0.60 else 'unsure'
    return {'decision': decision, 'value': value, 'conf': conf, 'evidence': evidence, 'quote_found': quote}


def tag(changes, conf, evidence=None, quote=False):
    return {'changes_behavior': changes, 'evidence': evidence, 'quote_found': quote, 'conf': conf}


def files_reading(change=(), new=(), conf=None, complete=None):
    return {'change': list(change), 'new': list(new), 'complete': complete,
            'conf': {'change': conf if change else None, 'new': conf if new else None, 'complete': None}}


def readings(fields=None, tags=None, files=None):
    out = {'files': files or files_reading(), 'tags': dict(tags or {})}
    out.update(fields or {})
    return out


def pairs(entries):
    return {(e['path'], e['src']) for e in entries}


DOCS_TRIVIAL = {'type': 'docs', 'difficulty': 'trivial', 'needs_review': False, 'ladder': ('G-l',)}
TEST_MEDIUM = {'type': 'test', 'needs_review': False, 'ladder': ('G-h', 'G-x')}
EASY = {'difficulty': 'easy', 'ladder': ('G-m', 'G-h', 'S')}
HARD = {'difficulty': 'hard', 'ladder': ('G-x', 'S+', 'O')}
DESIGN = {'route': 'design', 'ladder': ()}
AUTH_FACTS = make_facts(risk=(AUTH_TEXT,))
DOCS_Q = 'docs only wording'

STRICTER = [
    ('difficulty up at raise_p', {'rd': readings({'difficulty': rd(3, 0.62)})},
     {'difficulty': 'hard', 'cell': ('code', 'hard')}),
    ('difficulty up below raise_p', {'rd': readings({'difficulty': rd(3, 0.55)})}, {'difficulty': 'medium'}),
    ('risk up at raise_p', {'rd': readings({'risk': rd(2, 0.62)})}, {'risk': 'high'}),
    ('risk up below raise_p', {'rd': readings({'risk': rd(2, 0.55)})}, {'risk': 'low'}),
    ('route cascade->design', {'rd': readings({'route_class': rd('design', 0.65)})},
     {'route': 'design', 'ladder': ()}),
    ('route cascade->judgment', {'rd': readings({'route_class': rd('judgment', 0.65)})},
     {'route': 'judgment', 'ladder': ('S+',)}),
    ('review on at raise_p', {'r': DOCS_TRIVIAL, 'rd': readings({'needs_review': rd(True, 0.62)})},
     {'needs_review': True}),
    ('text tag changes_behavior at raise_p',
     {'f': AUTH_FACTS, 'rd': readings(tags={'auth': tag(True, 0.62, 'login token', True)})},
     {'has_tag': 'auth', 'risk': 'high'}),
]
NEUTRAL = [
    ('type test->code at act_p', {'r': TEST_MEDIUM, 'rd': readings({'task_type': rd('code', 0.75)})},
     {'type': 'code', 'cell': ('code', 'medium'), 'needs_review': True}),
    ('type test->code below act_p', {'r': TEST_MEDIUM, 'rd': readings({'task_type': rd('code', 0.70)})},
     {'type': 'test'}),
    ('type test->code over the hint at act_p',
     {'r': TEST_MEDIUM, 'p': {'type': 'test'}, 'rd': readings({'task_type': rd('code', 0.80)})}, {'type': 'code'}),
    ('type bugfix->code is neutral',
     {'r': {'type': 'bugfix', 'ladder': ('G-x', 'S', 'O')}, 'rd': readings({'task_type': rd('code', 0.80)})},
     {'type': 'code', 'cell': ('code', 'medium')}),
]
LOOSER = [
    ('difficulty down without quote', {'rd': readings({'difficulty': rd(1, 0.9)})},
     {'difficulty': 'medium', 'gaps': True}),
    ('difficulty down with quote', {'rd': readings({'difficulty': rd(1, 0.9, Q, True)})},
     {'difficulty': 'easy', 'cell': ('code', 'easy')}),
    ('difficulty down with quote below act_p', {'rd': readings({'difficulty': rd(1, 0.7, Q, True)})},
     {'difficulty': 'medium'}),
    ('risk down without quote', {'r': {'risk': 'medium'}, 'rd': readings({'risk': rd(0, 0.9)})},
     {'risk': 'medium', 'gaps': True}),
    ('risk down with quote', {'r': {'risk': 'medium'}, 'rd': readings({'risk': rd(0, 0.9, DOCS_Q, True)})},
     {'risk': 'low'}),
    ('route design->cascade without quote', {'r': DESIGN, 'rd': readings({'route_class': rd('cascade', 0.9)})},
     {'route': 'design', 'ladder': ()}),
    ('route design->cascade with quote',
     {'r': DESIGN, 'rd': readings({'route_class': rd('cascade', 0.9, Q, True)})},
     {'route': 'cascade', 'cell': ('code', 'medium')}),
    ('type code->docs at medium without quote', {'rd': readings({'task_type': rd('docs', 0.9)})},
     {'type': 'code', 'gaps': True}),
    ('type code->docs at medium with quote', {'rd': readings({'task_type': rd('docs', 0.9, DOCS_Q, True)})},
     {'type': 'docs', 'cell': ('docs', 'medium')}),
    ('text tag mention_only without quote',
     {'f': AUTH_FACTS, 'rd': readings(tags={'auth': tag(False, 0.9, 'login token')})},
     {'risk_in': ('medium', 'high'), 'gaps': True}),
]
ABSTAIN = [
    ('type hint vs abstain band', {'p': {'type': 'test'}, 'rd': readings({'task_type': rd('code', 0.6)})},
     {'type': 'test'}),
    ('difficulty hint vs abstain band',
     {'p': {'difficulty': 'medium'}, 'rd': readings({'difficulty': rd(1, 0.7, Q, True)})}, {'difficulty': 'medium'}),
    ('route hint vs stricter at raise_p',
     {'p': {'route': 'cascade'}, 'rd': readings({'route_class': rd('design', 0.65)})}, {'route': 'design'}),
    ('type hint beats rules', {'p': {'type': 'test'}}, {'type': 'test', 'cell': ('test', 'medium')}),
    ('difficulty hint beats rules', {'p': {'difficulty': 'hard'}}, {'difficulty': 'hard', 'cell': ('code', 'hard')}),
]
PINS = [
    ('pinned type ignores judge', {'r': TEST_MEDIUM, 'p': {'type': 'test', 'pin': ('type',)},
                                   'rd': readings({'task_type': rd('code', 0.95, Q, True)})}, {'type': 'test'}),
    ('pinned difficulty vs judge up', {'r': EASY, 'p': {'difficulty': 'easy', 'pin': ('difficulty',)},
                                       'rd': readings({'difficulty': rd(3, 0.9)})}, {'difficulty': 'easy'}),
    ('pinned difficulty under the F3 floor', {'r': EASY, 'p': {'difficulty': 'easy', 'pin': ('difficulty',)},
                                              'f': make_facts(many_named(8))}, {'difficulty': 'hard'}),
    ('pinned route ignores judge', {'p': {'route': 'cascade', 'pin': ('route',)},
                                    'rd': readings({'route_class': rd('design', 0.9)})}, {'route': 'cascade'}),
]
FLOORS = [
    ('path auth beats judge risk low at 0.99',
     {'f': make_facts(risk=(AUTH_PATH,)), 'rd': readings({'risk': rd(0, 0.99, Q, True)})},
     {'risk': 'high', 'has_tag': 'auth', 'm.strong_review': True, 'needs_review': True}),
    ('path auth forces review on docs', {'r': DOCS_TRIVIAL, 'f': make_facts(risk=(AUTH_PATH,))},
     {'needs_review': True, 'm.strong_review': True}),
    ('path deps floor', {'f': make_facts(risk=(DEPS_PATH,)), 'rd': readings({'risk': rd(0, 0.99, Q, True)})},
     {'risk': 'medium', 'has_tag': 'deps'}),
    ('eight named files floor',
     {'r': EASY, 'f': make_facts(many_named(8)), 'rd': readings({'difficulty': rd(0, 0.99, Q, True)})},
     {'difficulty': 'hard'}),
    ('path tag survives judge mention_only',
     {'f': make_facts(risk=(AUTH_PATH, AUTH_TEXT)), 'rd': readings(tags={'auth': tag(False, 0.95, 'login token', True)})},
     {'has_tag': 'auth', 'risk': 'high'}),
]
ABSENT = [
    ('absent: text auth counts as medium', {'f': AUTH_FACTS, 'rd': None},
     {'risk': 'medium', 'needs_review': True, 'm.strong_review': True, 'm.judge_absent': True,
      'm.fallback': 'rules'}),
    ('unsure text tag counts as medium', {'f': AUTH_FACTS, 'rd': readings(tags={'auth': tag(None, 0.55)})},
     {'risk': 'medium', 'm.strong_review': True, 'm.judge_absent': False, 'm.fallback': None}),
    ('absent: hint beats rules', {'p': {'type': 'test'}, 'rd': None}, {'type': 'test', 'm.judge_absent': True}),
    ('absent: path floor applies', {'f': make_facts(risk=(AUTH_PATH,)), 'rd': None},
     {'risk': 'high', 'has_tag': 'auth'}),
]
CLASSIFIER = [
    ('classifier never lowers difficulty',
     {'src': 'classifier', 'rd': readings({'difficulty': rd(1, 0.95, Q, True)})},
     {'difficulty': 'medium', 'm.fallback': 'classifier'}),
    ('classifier never turns code into docs',
     {'src': 'classifier', 'rd': readings({'task_type': rd('docs', 0.95, DOCS_Q, True)})}, {'type': 'code'}),
    ('classifier never leaves design',
     {'src': 'classifier', 'r': DESIGN, 'rd': readings({'route_class': rd('cascade', 0.95, Q, True)})},
     {'route': 'design'}),
    ('classifier may raise risk', {'src': 'classifier', 'rd': readings({'risk': rd(2, 0.9)})}, {'risk': 'high'}),
    ('classifier neutral type change',
     {'src': 'classifier', 'r': TEST_MEDIUM, 'rd': readings({'task_type': rd('code', 0.8)})}, {'type': 'code'}),
]
GAP = [
    ('gap up: judge higher', {'r': EASY, 'rd': readings({'difficulty': rd(3, 0.65)})},
     {'difficulty': 'hard', 'gap': 'difficulty_gap:easy->hard'}),
    ('gap down: rules higher', {'r': HARD, 'rd': readings({'difficulty': rd(1, 0.9, Q, True)})},
     {'difficulty': 'hard', 'gap': 'difficulty_gap:hard->easy'}),
]


def random_case(rng):
    def conf():
        return round(rng.uniform(0.4, 1.0), 2)

    def field(value):
        quote = rng.random() < 0.5
        return rd(value, conf(), 'x' if quote else None, quote)

    path_tags = rng.sample(TAGS, rng.randint(0, 2))
    text_tags = rng.sample(TAGS, rng.randint(0, 2))
    n_named = rng.choice((0, 2, 8, 10))
    risk = tuple(RiskItem(t, 'path', None, 'src/p_%s.py' % t) for t in path_tags)
    risk += tuple(RiskItem(t, 'text', t, None) for t in text_tags)
    params = {'budget': rng.choice(('normal', 'low', 'free'))}
    for name, values in (('type', TYPES), ('difficulty', DIFF), ('route', ('cascade', 'judgment', 'design'))):
        if rng.random() < 0.4:
            params[name] = rng.choice(values)
    pin = tuple(f for f in ('type', 'difficulty', 'route') if f in params and rng.random() < 0.5)
    if pin:
        params['pin'] = pin
    reading = None
    if rng.random() >= 0.2:
        fields = {'task_type': field(rng.choice(TYPES)), 'difficulty': field(rng.randint(0, 4)),
                  'risk': field(rng.randint(0, 2)), 'route_class': field(rng.choice(('cascade', 'design', 'judgment'))),
                  'needs_review': field(rng.random() < 0.5)}
        fields = {k: v for k, v in fields.items() if rng.random() < 0.8}
        tags = {t: tag(rng.choice((True, False, None)), conf(), t, rng.random() < 0.5) for t in text_tags}
        reading = readings(fields, tags, files_reading((), ['src/brand_new.py'], conf(), rng.choice((True, False, None))))
    rules_d = rules(type=rng.choice(TYPES), difficulty=rng.choice(DIFF), risk=rng.choice(RISK),
                    route=rng.choice(('cascade', 'cascade', 'design', 'judgment')), ladder=(),
                    risk_tags=frozenset(rng.sample(TAGS, rng.randint(0, 2))), needs_review=rng.random() < 0.5)
    kw = {'rules_d': rules_d, 'params': params, 'facts': make_facts(many_named(n_named), risk), 'rd_': reading,
          'source': rng.choice(('judge', 'judge', 'classifier'))}
    return kw, set(path_tags), n_named


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        home = os.path.join(self.root, 'home')
        os.makedirs(home)
        env = patch.dict(os.environ, {'MR_HOME': home})
        env.start()
        self.addCleanup(env.stop)
        self.cfg = from_dict(defaults())

    def mod(self):
        try:
            return importlib.import_module('mr.judge.merge')
        except ModuleNotFoundError as exc:
            self.fail(f'mr.judge.merge is missing: {exc}')

    def merge(self, rules_d=None, params=None, facts=None, rd_=None, source='judge', cfg=None):
        fn = getattr(self.mod(), 'merge', None)
        self.assertTrue(callable(fn), 'mr.judge.merge.merge is missing')
        task = Task(id='t-0000aj03', text=TEXT, repo=self.root, base='0' * 40, params=Params(**(params or {})),
                    files_named=(), source='cli')
        return fn(task, facts if facts is not None else make_facts(), rules_d if rules_d is not None else rules(),
                  rd_, cfg or self.cfg, source=source)

    def run_table(self, table):
        for name, kw, expect in table:
            m = self.merge(rules(**kw.get('r', {})), kw.get('p'), kw.get('f'),
                           kw['rd'] if 'rd' in kw else readings(), kw.get('src', 'judge'))
            self.check(name, m, expect)

    def check(self, name, m, expect):
        d = m.decision
        for key, want in expect.items():
            if key == 'has_tag':
                self.assertIn(want, d.risk_tags, name)
            elif key == 'gap':
                self.assertIn(want, m.gaps, name)
            elif key == 'gaps':
                self.assertTrue(m.gaps, f'{name}: the ignored answer is not logged in gaps')
            elif key == 'cell':
                self.assertEqual(d.ladder, tuple(self.cfg.ladders[want[0]][want[1]]), name)
            elif key == 'risk_in':
                self.assertIn(d.risk, want, name)
            elif key.startswith('m.'):
                self.assertEqual(getattr(m, key[2:]), want, f'{name}: {key}')
            else:
                self.assertEqual(getattr(d, key), want, f'{name}: {key}')


class BarsTest(_Base):
    def test_stricter_answers_apply_at_raise_p(self):
        self.run_table(STRICTER)

    def test_neutral_answers_need_act_p(self):
        self.run_table(NEUTRAL)

    def test_looser_answers_need_act_p_and_a_found_quote(self):
        self.run_table(LOOSER)

    def test_abstain_band_never_overrides_a_hint(self):
        self.run_table(ABSTAIN)

    def test_pinned_field_keeps_hint_and_floors_still_apply(self):
        self.run_table(PINS)

    def test_safety_floors_beat_the_judge(self):
        self.run_table(FLOORS)

    def test_absent_or_unsure_text_tag_counts_as_medium(self):
        self.run_table(ABSENT)

    def test_classifier_source_never_loosens(self):
        self.run_table(CLASSIFIER)

    def test_difficulty_gap_higher_wins_and_is_logged(self):
        self.run_table(GAP)


class MergeOutputTest(_Base):
    def test_text_tag_mention_only_with_quote_drops_and_is_logged(self):
        m = self.merge(rules(risk_tags=frozenset({'auth'})), None, AUTH_FACTS,
                       readings(tags={'auth': tag(False, 0.9, 'login token', True)}))
        self.assertNotIn('auth', m.decision.risk_tags)
        self.assertEqual((m.decision.risk, m.strong_review), ('low', False))
        logged = [e for e in m.risk_evidence if e.get('tag') == 'auth' and e.get('source') == 'text']
        self.assertTrue(logged, m.risk_evidence)
        self.assertEqual(logged[0].get('judge'), 'mention_only', logged)

    def test_overrides_one_per_field_changed_from_hint(self):
        mod = self.mod()
        self.assertEqual([f.name for f in dataclasses.fields(mod.Override)],
                         ['field', 'hint', 'applied', 'judge_conf', 'evidence'])
        want = {'decision', 'overrides', 'risk_evidence', 'files', 'judge_absent', 'fallback', 'gaps', 'strong_review'}
        self.assertTrue(want <= {f.name for f in dataclasses.fields(mod.Merged)})
        m = self.merge(rules(**TEST_MEDIUM), {'type': 'test'}, None, readings({'task_type': rd('code', 0.8)}))
        self.assertIsInstance(m.overrides, tuple)
        got = [o for o in m.overrides if o.field == 'type']
        self.assertEqual(len(got), 1, m.overrides)
        self.assertEqual((got[0].hint, got[0].applied, got[0].evidence), ('test', 'code', None))
        self.assertAlmostEqual(got[0].judge_conf, 0.8)
        m = self.merge(rules(), {'difficulty': 'medium'}, None, readings({'difficulty': rd(1, 0.9, Q, True)}))
        got = [o for o in m.overrides if o.field == 'difficulty']
        self.assertEqual(len(got), 1, m.overrides)
        self.assertEqual((got[0].hint, got[0].applied, got[0].evidence), ('medium', 'easy', Q))
        self.assertAlmostEqual(got[0].judge_conf, 0.9)
        m = self.merge(rules(**TEST_MEDIUM), {'type': 'test'}, None, readings({'task_type': rd('code', 0.6)}))
        self.assertEqual([o for o in m.overrides if o.field == 'type'], [])

    def test_files_sources_and_act_p_bar(self):
        facts = make_facts((APP, CALLER, UTIL, NEW))
        m = self.merge(None, None, facts, readings(files=files_reading(['src/util.py'], ['src/extra.py'], 0.8, True)))
        self.assertEqual(pairs(m.files['change']),
                         {('src/app.py', 'named'), ('src/caller.py', 'hint'), ('src/util.py', 'judge')})
        self.assertEqual(pairs(m.files['new']), {('src/new_mod.py', 'named'), ('src/extra.py', 'judge')})
        self.assertIsInstance(m.files['partial'], bool)
        self.assertEqual(m.decision.files, m.files)
        low = readings(files=files_reading(['src/util.py'], ['src/extra.py'], 0.7, True))
        for reading in (low, None):
            m = self.merge(None, None, facts, reading)
            self.assertEqual(pairs(m.files['change']), {('src/app.py', 'named'), ('src/caller.py', 'hint')})
            self.assertEqual(pairs(m.files['new']), {('src/new_mod.py', 'named')})

    def test_ladder_recomputed_with_prefer_and_budget(self):
        up = readings({'difficulty': rd(3, 0.65)})
        self.assertEqual(self.merge(None, {'budget': 'low'}, None, up).decision.ladder, ('G-x', 'S+'))
        self.assertEqual(self.merge(None, {'budget': 'free'}, None, readings()).decision.ladder, ('G-h', 'G-x'))
        self.assertEqual(self.merge(None, {'prefer': 'S'}, None, readings()).decision.ladder, ('S', 'G-h', 'G-x'))
        d = self.merge(None, None, None, up).decision
        self.assertEqual((d.ladder, d.needs_review), (('G-x', 'S+', 'O'), True))

    def test_invariant_violation_raises_invariant_error(self):
        self.mod()
        cfg = dataclasses.replace(self.cfg, allowed=('grok-4.7-*',))
        with self.assertRaises(router.InvariantError):
            self.merge(None, None, None, readings(), cfg=cfg)

    def test_property_300_seeded_cases_keep_invariants(self):
        self.mod()
        rng = random.Random(20261103)
        for i in range(300):
            kw, path_tags, n_named = random_case(rng)
            msg = f'case {i}: {kw}'
            try:
                m = self.merge(**kw)
            except router.InvariantError as exc:
                self.fail(f'{msg} raised InvariantError {exc}')
            d, params = m.decision, kw['params']
            floor = 2 if path_tags & HIGH else 1 if path_tags & MED else 0
            self.assertGreaterEqual(RISK.index(d.risk), floor, msg)
            self.assertTrue(path_tags <= set(d.risk_tags), msg)
            if path_tags & HIGH:
                self.assertTrue(m.strong_review and d.needs_review, msg)
            if n_named >= 8:
                self.assertGreaterEqual(DIFF.index(d.difficulty), DIFF.index('hard'), msg)
            for code in d.ladder:
                self.assertIn(code, self.cfg.rungs, msg)
            if params['budget'] == 'low':
                self.assertFalse(set(d.ladder) & {'O', 'O+'}, msg)
            if params['budget'] == 'free':
                self.assertFalse([c for c in d.ladder if self.cfg.rungs[c].path == 'max'], msg)
            if d.route == 'design':
                self.assertEqual(d.ladder, (), msg)
            for name in params.get('pin', ()):
                if name in ('type', 'route') or n_named < 8:
                    self.assertEqual(getattr(d, name), params[name], msg)


if __name__ == '__main__':
    unittest.main()
