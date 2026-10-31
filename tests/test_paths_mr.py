from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _env(home, **extra):
    base = {
        'HOME': home,
        'MR_CONFIG': '',
        'MR_HOME': '',
        'XDG_CONFIG_HOME': '',
        'XDG_DATA_HOME': '',
        'MR_TEST_ALLOW_FAKE_PATHS': '',
    }
    base.update(extra)
    return base


def _paths():
    try:
        from mr.paths import config_dir, data_dir, default_config_path
    except ImportError:
        raise AssertionError('mr.paths is missing')
    return config_dir, data_dir, default_config_path


class TestPortableConfig(unittest.TestCase):
    def test_config_dir_data_dir_and_default_under_home(self):
        config_dir, data_dir, default_config_path = _paths()
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, _env(home), clear=False):
                self.assertEqual(config_dir(), os.path.join(home, '.config', 'mr'))
                self.assertEqual(data_dir(), os.path.join(home, '.local', 'share', 'mr'))
                self.assertEqual(
                    default_config_path(),
                    os.path.join(home, '.config', 'mr', 'routes.yaml'),
                )
                self.assertTrue(config_dir().startswith(home + os.sep))
                self.assertTrue(data_dir().startswith(home + os.sep))
                self.assertTrue(default_config_path().startswith(home + os.sep))

    def test_mr_home_beats_xdg_and_tilde_and_empty(self):
        config_dir, data_dir, _default = _paths()
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, _env(
                home,
                MR_HOME=os.path.join(home, 'from-mr'),
                XDG_DATA_HOME=os.path.join(home, 'from-xdg'),
                XDG_CONFIG_HOME=os.path.join(home, 'cfg'),
            ), clear=False):
                self.assertEqual(data_dir(), os.path.join(home, 'from-mr'))
                self.assertEqual(config_dir(), os.path.join(home, 'cfg', 'mr'))
            with mock.patch.dict(os.environ, _env(
                home,
                MR_HOME='~/mrh',
                XDG_DATA_HOME=os.path.join(home, 'from-xdg'),
                XDG_CONFIG_HOME='~/xdgc',
            ), clear=False):
                self.assertEqual(data_dir(), os.path.join(home, 'mrh'))
                self.assertEqual(config_dir(), os.path.join(home, 'xdgc', 'mr'))
            with mock.patch.dict(os.environ, _env(
                home,
                XDG_DATA_HOME=os.path.join(home, 'only-xdg'),
            ), clear=False):
                self.assertEqual(data_dir(), os.path.join(home, 'only-xdg', 'mr'))
                self.assertEqual(config_dir(), os.path.join(home, '.config', 'mr'))

    def test_env_is_read_at_call_time(self):
        _config_dir, data_dir, _default = _paths()
        with tempfile.TemporaryDirectory() as first_home:
            with tempfile.TemporaryDirectory() as second_home:
                with mock.patch.dict(os.environ, _env(first_home), clear=False):
                    first = data_dir()
                with mock.patch.dict(os.environ, _env(second_home), clear=False):
                    second = data_dir()
                self.assertEqual(first, os.path.join(first_home, '.local', 'share', 'mr'))
                self.assertEqual(second, os.path.join(second_home, '.local', 'share', 'mr'))
                self.assertNotEqual(first, second)

    def test_builtin_yaml_is_public(self):
        from mr.defaults import DEFAULT_YAML, defaults
        raw = defaults()
        self.assertEqual(raw['paths']['claude'], 'claude')
        self.assertEqual(raw['paths']['check'], '')
        self.assertEqual(raw['paths']['home'], '')
        self.assertEqual(raw['hygiene']['commands'], {})
        self.assertEqual(raw['judge']['repos'], [])
        self.assertIsNone(re.search(r'/(home|Users)/[A-Za-z]', DEFAULT_YAML))
        self.assertNotIn('30 Oct', DEFAULT_YAML)
        self.assertIsInstance(DEFAULT_YAML, str)
        self.assertEqual(raw['paths']['cursor_agent'], 'cursor-agent')
        self.assertEqual(raw['version'], 1)
        self.assertEqual(raw['claude_limits'], {'hour_max': None, 'five_hour_max': 85})
        self.assertEqual(raw['rungs']['G-l']['model'], 'grok-4.7-low')
        self.assertEqual(raw['rungs']['S']['effort'], 'high')
        self.assertEqual(raw['caps']['opus_rungs_per_task'], 1)
        self.assertEqual(raw['advisor']['mode'], 'on')
        self.assertEqual(raw['judge']['mode'], 'off')
        self.assertEqual(raw['judge']['backend'], 'haiku')
        self.assertIn('judge:max:claude-haiku-5-5', raw['allowed'])
        self.assertEqual(raw['fallback_claude_denied'], ['G-x'])
        self.assertEqual(len(raw['ladders']), 10)

    def test_docstring_is_not_a_spec_mirror(self):
        import mr.defaults as defaults
        doc = defaults.__doc__ or ''
        self.assertNotIn('verbatim', doc.lower())
        self.assertNotIn('4.3', doc)
        src = Path(defaults.__file__).read_text(encoding='utf-8')
        self.assertIn('DEFAULT_YAML: str', src)
        self.assertNotIn('open(', src)

    def test_machine_constants_removed(self):
        import mr.config as config
        self.assertFalse(hasattr(config, 'DEFAULT_PATH'))
        self.assertFalse(hasattr(config, 'CLAUDE_PATH'))

    def test_load_without_file_resolves_home(self):
        from mr.config import load
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, _env(home), clear=False):
                try:
                    cfg = load()
                except Exception as exc:
                    self.fail('absent default config should load built-ins: ' + str(exc))
            self.assertEqual(cfg.source, '<defaults>')
            self.assertEqual(cfg.paths.claude, 'claude')
            self.assertEqual(cfg.paths.check, '')
            self.assertEqual(cfg.paths.home, os.path.join(home, '.local', 'share', 'mr'))

    def test_xdg_routes_used_when_mr_config_empty(self):
        from mr.config import load
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, '.config', 'mr', 'routes.yaml')
            os.makedirs(os.path.dirname(path))
            Path(path).write_text(
                'caps:\n  attempts:\n    trivial: 9\n', encoding='utf-8')
            with mock.patch.dict(os.environ, _env(home), clear=False):
                try:
                    cfg = load()
                except Exception as exc:
                    self.fail('load() should read the XDG routes file: ' + str(exc))
            self.assertEqual(cfg.source, path)
            self.assertEqual(cfg.caps.attempts['trivial'], 9)
            self.assertEqual(cfg.caps.attempts['easy'], 3)

    def test_relative_check_rejected(self):
        from mr.config import ConfigError, from_dict
        from mr.defaults import defaults
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, _env(home), clear=False):
                for rel in ('relative-check', 'rel/check.sh'):
                    with self.subTest(check=rel):
                        raw = copy.deepcopy(defaults())
                        raw['paths']['claude'] = 'claude'
                        raw['paths']['cursor_agent'] = 'cursor-agent'
                        raw['paths']['home'] = home
                        raw['paths']['check'] = rel
                        with self.assertRaises(ConfigError) as caught:
                            from_dict(raw)
                        msg = str(caught.exception)
                        self.assertIn('paths.check', msg)
                        self.assertNotIn('paths.claude', msg)

    def test_bare_and_other_absolute_claude_accepted(self):
        from mr.config import ConfigError, from_dict
        from mr.defaults import defaults
        with mock.patch.dict(os.environ, {'MR_TEST_ALLOW_FAKE_PATHS': ''}, clear=False):
            raw = copy.deepcopy(defaults())
            raw['paths']['claude'] = 'claude'
            try:
                cfg = from_dict(raw)
            except ConfigError as exc:
                self.fail('bare paths.claude should load: ' + str(exc))
            self.assertEqual(cfg.paths.claude, 'claude')
            self.assertEqual(cfg.paths.cursor_agent, 'cursor-agent')
            self.assertNotIn('MR_TEST_ALLOW_FAKE_PATHS set', cfg.warnings)
            raw = copy.deepcopy(defaults())
            raw['paths']['claude'] = '/tmp/other-claude'
            try:
                cfg = from_dict(raw)
            except ConfigError as exc:
                self.fail('absolute paths.claude should load: ' + str(exc))
            self.assertEqual(cfg.paths.claude, '/tmp/other-claude')
            self.assertNotIn('MR_TEST_ALLOW_FAKE_PATHS set', cfg.warnings)

    def test_relative_home_rejected_with_bare_claude(self):
        from mr.config import ConfigError, from_dict
        from mr.defaults import defaults
        with mock.patch.dict(os.environ, {'MR_TEST_ALLOW_FAKE_PATHS': ''}, clear=False):
            raw = copy.deepcopy(defaults())
            raw['paths']['claude'] = 'claude'
            raw['paths']['check'] = '/tmp/check.sh'
            raw['paths']['cursor_agent'] = 'cursor-agent'
            raw['paths']['home'] = 'rel/home'
            with self.assertRaises(ConfigError) as caught:
                from_dict(raw)
            msg = str(caught.exception)
            self.assertIn('paths.home', msg)
            self.assertNotIn('paths.claude', msg)

    def test_cursor_agent_with_slash_rejected(self):
        from mr.config import ConfigError, from_dict
        from mr.defaults import defaults
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, _env(home), clear=False):
                raw = copy.deepcopy(defaults())
                raw['paths']['claude'] = 'claude'
                raw['paths']['check'] = '/tmp/check.sh'
                raw['paths']['home'] = home
                raw['paths']['cursor_agent'] = 'dir/cursor-agent'
                with self.assertRaises(ConfigError) as caught:
                    from_dict(raw)
                msg = str(caught.exception)
                self.assertIn('paths.cursor_agent', msg)
                self.assertIn('bare command name', msg)

    def test_empty_home_resolves_to_data_dir(self):
        from mr.config import ConfigError, from_dict
        from mr.defaults import defaults
        with tempfile.TemporaryDirectory() as home:
            target = os.path.join(home, 'data-root')
            with mock.patch.dict(os.environ, _env(home, MR_HOME=target), clear=False):
                raw = copy.deepcopy(defaults())
                raw['paths']['claude'] = 'claude'
                raw['paths']['check'] = ''
                raw['paths']['home'] = ''
                try:
                    cfg = from_dict(raw)
                except ConfigError as exc:
                    self.fail('empty paths.home should resolve: ' + str(exc))
                self.assertEqual(cfg.paths.home, target)
                self.assertEqual(cfg.paths.check, '')
                raw['paths']['home'] = '/tmp/keep-home'
                try:
                    cfg = from_dict(raw)
                except ConfigError as exc:
                    self.fail('absolute paths.home should load: ' + str(exc))
                self.assertEqual(cfg.paths.home, '/tmp/keep-home')
                self.assertEqual(cfg.paths.check, '')

    def test_empty_check_guard_allows_without_subprocess(self):
        from mr.config import ConfigError, from_dict
        from mr.defaults import defaults
        import mr.guard as guard
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, _env(home), clear=False):
                raw = copy.deepcopy(defaults())
                raw['paths']['claude'] = 'claude'
                raw['paths']['check'] = ''
                raw['paths']['home'] = home
                try:
                    cfg = from_dict(raw)
                except ConfigError as exc:
                    self.fail('empty paths.check should load: ' + str(exc))
                with mock.patch('mr.guard.subprocess.run') as run:
                    res = guard.check('S', cfg, now=1700000000.0)
                    other = guard.check('not-a-rung', cfg, now=1700000000.0)
                self.assertEqual(run.call_count, 0)
                self.assertTrue(res.allow)
                self.assertEqual(res.reason, 'no_check')
                self.assertIsNone(res.hour)
                self.assertIsNone(res.five_hour)
                self.assertTrue(other.allow)
                self.assertEqual(other.reason, 'no_check')
                self.assertIsNone(other.hour)
                self.assertIsNone(other.five_hour)

    def test_empty_check_guarded_run_skips_probe_and_releases(self):
        from mr.config import ConfigError, from_dict
        from mr.defaults import defaults
        import mr.guard as guard
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, _env(home), clear=False):
                raw = copy.deepcopy(defaults())
                raw['paths']['claude'] = 'claude'
                raw['paths']['check'] = ''
                raw['paths']['home'] = home
                try:
                    cfg = from_dict(raw)
                except ConfigError as exc:
                    self.fail('empty paths.check should load: ' + str(exc))
                seen = []

                def exec_fn(argv):
                    seen.append(list(argv))
                    return type('R', (), {'rc': 0, 'stderr': '', 'result_text': ''})()

                with mock.patch('mr.guard.subprocess.run') as run:
                    out = guard.run_claude_guarded(
                        'S', ['-p', 'hi'], cfg, task='t-1', exec_fn=exec_fn, wait_lock_s=0)
                self.assertEqual(run.call_count, 0)
                self.assertEqual(seen, [['claude', '-p', 'hi']])
                self.assertTrue(out.guard.allow)
                self.assertEqual(out.guard.reason, 'no_check')
                self.assertIsNone(out.guard.hour)
                self.assertIsNone(out.guard.five_hour)
                self.assertIsNone(out.delta)
                self.assertIsNotNone(out.result)
                res_path = os.path.join(home, 'state', 'reservations.json')
                self.assertTrue(os.path.isfile(res_path))
                data = json.loads(Path(res_path).read_text(encoding='utf-8'))
                self.assertEqual(data.get('reservations'), [])

    def test_existing_tests_drop_machine_pins(self):
        root = Path(__file__).resolve().parents[1]
        cfg_text = (root / 'tests' / 'test_config.py').read_text(encoding='utf-8')
        runners = (root / 'tests' / 'test_runners.py').read_text(encoding='utf-8')
        self.assertIsNone(re.search(r'/(home|Users)/[A-Za-z]', cfg_text))
        self.assertIsNone(re.search(r'/(home|Users)/[A-Za-z]', runners))
        self.assertIn('argv[0], "claude"', runners)
        self.assertIn('cursor:claude-haiku-5-5', cfg_text)


if __name__ == '__main__':
    unittest.main()
