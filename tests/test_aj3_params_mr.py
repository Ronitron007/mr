'''AJ3 locked tests: router.Decision gains files / risk_evidence / judge_status with safe defaults, and
Params.pin comes from task JSON params.pin and the repeatable --pin flag on run and route. No claude, no network.'''
import dataclasses
import importlib
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from mr import router
from mr.task import Params, TaskError, load_task
from tests import gitfix

MISSING = object()
BASE = dict(route='cascade', type='code', difficulty='medium', risk='low', risk_tags=frozenset({'deps'}),
            load_bearing=False, high_judgment=False, needs_review=True, isolate_hint=None,
            needs_clarification=False, ladder=('G-h', 'G-x', 'S'), reasons=(), sources={'route': 'default'})


def _plain(obj):
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, 'home')
        os.makedirs(self.home)
        env = patch.dict(os.environ, {'MR_HOME': self.home})
        env.start()
        self.addCleanup(env.stop)

    def task_file(self, params):
        path = os.path.join(self.root, 'task.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'text': 'Fix the token check in src/app.py', 'params': params}, fh)
        return path

    def repo(self):
        return gitfix.make_repo(os.path.join(self.root, 'repo'), {'src/app.py': 'x = 1' + chr(10)})


class DecisionFieldsTest(_Base):
    def test_new_fields_have_json_safe_defaults(self):
        d = router.Decision(**BASE)
        self.assertEqual(getattr(d, 'files', MISSING), {})
        self.assertEqual(getattr(d, 'risk_evidence', MISSING), ())
        self.assertIsNone(getattr(d, 'judge_status', MISSING))
        self.assertEqual(d, router.Decision(**BASE))
        doc = json.loads(json.dumps(_plain(dataclasses.asdict(d))))
        self.assertEqual((doc['files'], doc['risk_evidence'], doc['judge_status']), ({}, [], None))

    def test_new_fields_accept_merged_values(self):
        files = {'change': [{'path': 'src/app.py', 'src': 'named'}], 'new': [], 'partial': False}
        ev = ({'tag': 'auth', 'source': 'path', 'path': 'src/auth/session.py'},)
        try:
            d = router.Decision(**BASE, files=files, risk_evidence=ev, judge_status='ok')
        except TypeError as exc:
            self.fail(f'Decision lacks the AJ3 fields: {exc}')
        self.assertEqual((d.files, d.risk_evidence, d.judge_status), (files, ev, 'ok'))
        self.assertNotEqual(d, router.Decision(**BASE))


class PinTest(_Base):
    def test_task_file_pin_loads_as_tuple(self):
        self.assertEqual(getattr(Params(), 'pin', MISSING), ())
        path = self.task_file({'type': 'test', 'pin': ['type', 'difficulty']})
        task = load_task(None, path, Params(), self.repo(), home=self.home)
        self.assertEqual(getattr(task.params, 'pin', MISSING), ('type', 'difficulty'))
        self.assertEqual(task.params.type, 'test')

    def test_task_file_pin_rejects_unknown_field(self):
        path = self.task_file({'pin': ['type', 'colour']})
        repo = self.repo()
        with self.assertRaises(TaskError):
            load_task(None, path, Params(), repo, home=self.home)

    def test_pin_flag_on_run_and_route(self):
        cli = importlib.import_module('mr.cli')
        for cmd in ('run', 'route'):
            build = importlib.import_module('mr.commands.' + cmd)._params
            try:
                args = cli._parse_argv(cmd, ['Fix src/app.py', '--pin', 'type', '--pin', 'route'])
            except Exception as exc:
                self.fail(f'mr {cmd} --pin was rejected: {exc!r}')
            self.assertEqual(getattr(build(args), 'pin', MISSING), ('type', 'route'), cmd)
            plain = build(cli._parse_argv(cmd, ['Fix src/app.py']))
            self.assertEqual(getattr(plain, 'pin', MISSING), (), cmd)


if __name__ == '__main__':
    unittest.main()
