'''SHD2 locked tests: mr.classifier prompt template and strict reader (parse).'''
import dataclasses
import hashlib
import importlib
import json
import os
import unittest

import mr

VALID = {'task_type': 'bugfix', 'difficulty': 'easy', 'risk': 'low', 'risk_tags': ['none'],
         'load_bearing': False, 'high_judgment': False, 'scope_files_est': 2, 'ambiguity': 'low',
         'novelty': 'mixed', 'context': 'small', 'model': 'G-m', 'effort': 'medium',
         'needs_worktree': False, 'needs_review': False, 'needs_clarification': False,
         'confidence': 0.8, 'reason': 'small typo fix'}
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(mr.__file__)), 'prompts', 'classify.v1.txt')


def _clf(case):
    try:
        return importlib.import_module('mr.classifier')
    except ImportError as exc:
        case.fail(f'mr.classifier is missing: {exc}')


def _line(**kw):
    return json.dumps({**VALID, **kw}, separators=(',', ':'))


class PromptTest(unittest.TestCase):
    def test_prompt_version_and_sha_match_template(self):
        c = _clf(self)
        self.assertEqual(c.PROMPT_VERSION, 'classify.v1')
        with open(TEMPLATE, 'rb') as fh:
            want = hashlib.sha256(fh.read()).hexdigest()[:12]
        self.assertEqual(c.prompt_sha(), want)
        self.assertEqual(c.prompt_sha(), c.prompt_sha())
        self.assertRegex(c.prompt_sha(), '^[0-9a-f]{12}$')

    def test_template_is_generic(self):
        _clf(self)
        with open(TEMPLATE, encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('coding tasks in the repository', text)
        for word in ('Expo', 'React Native', 'Supabase'):
            self.assertNotIn(word, text)


class ParseTest(unittest.TestCase):
    def test_parse_valid_object(self):
        c = _clf(self)
        self.assertEqual({f.name for f in dataclasses.fields(c.ClassifierOut)}, set(VALID))
        out = c.parse(_line())
        self.assertIsInstance(out, c.ClassifierOut)
        self.assertEqual((out.task_type, out.difficulty, out.risk), ('bugfix', 'easy', 'low'))
        self.assertEqual(out.risk_tags, ())
        self.assertEqual(out.scope_files_est, 2)
        self.assertIs(out.load_bearing, False)
        self.assertEqual((out.model, out.effort), ('G-m', 'medium'))
        self.assertEqual(out.confidence, 0.8)
        self.assertEqual(out.reason, 'small typo fix')
        with self.assertRaises(dataclasses.FrozenInstanceError):
            out.task_type = 'docs'
        nulls = c.parse(_line(model=None, effort=None))
        self.assertIsNotNone(nulls)
        self.assertIsNone(nulls.model)
        self.assertIsNone(nulls.effort)

    def test_parse_takes_last_object_amid_prose(self):
        c = _clf(self)
        text = ('Let me think. Draft: ' + _line(task_type='docs') + ' Final answer: '
                + _line(task_type='test', risk_tags=['deps', 'none']) + ' Hope this helps.')
        out = c.parse(text)
        self.assertIsNotNone(out)
        self.assertEqual(out.task_type, 'test')
        self.assertEqual(out.risk_tags, ('deps',))
        pretty = c.parse('Answer: ' + json.dumps({**VALID, 'reason': 'see {x}'}, indent=2) + ' end')
        self.assertIsNotNone(pretty)
        self.assertEqual(pretty.reason, 'see {x}')

    def test_parse_clamps_confidence(self):
        c = _clf(self)
        self.assertEqual(c.parse(_line(confidence=1.7)).confidence, 1.0)
        self.assertEqual(c.parse(_line(confidence=-0.4)).confidence, 0.0)
        self.assertEqual(c.parse(_line(confidence=0.55)).confidence, 0.55)

    def test_parse_rejects_bad_values(self):
        c = _clf(self)
        missing = json.dumps({k: v for k, v in VALID.items() if k != 'risk'})
        bad = [_line(difficulty='extreme'), _line(task_type='poem'), _line(risk_tags=['weird']),
               _line(effort='max'), _line(scope_files_est=-1), _line(load_bearing='yes'),
               _line(confidence='high'), missing, _line()[:-1], 'no json here', '']
        for text in bad:
            with self.subTest(text=text[-60:]):
                self.assertIsNone(c.parse(text))

    def test_parse_ignores_extra_keys_and_cuts_reason(self):
        c = _clf(self)
        out = c.parse(_line(extra_key=1, reason='w' * 300))
        self.assertIsNotNone(out)
        self.assertEqual(len(out.reason), 120)
        self.assertFalse(hasattr(out, 'extra_key'))


if __name__ == '__main__':
    unittest.main()
