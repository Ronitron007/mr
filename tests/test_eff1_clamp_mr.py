"""EFF1 locked checks: Sonnet and Opus only at high or xhigh."""
from __future__ import annotations

import copy
import fnmatch
import os
import re
import unittest
from types import SimpleNamespace

import yaml

from mr import config, runners
from mr.defaults import DEFAULT_YAML, defaults

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))



def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _spec_yaml(text):
    rest = text[text.find("### 4.3"):]
    match = re.search(r"```yaml\n(.*?)\n```", rest, re.S)
    if not match:
        raise AssertionError("section 4.3 yaml fence not found")
    return match.group(1)


def _flag(argv):
    if "--effort" not in argv:
        return None
    return argv[argv.index("--effort") + 1]


def _tail(model, effort, advisor=None, code="X"):
    rung = config.Rung(code, model, "max", effort, advisor)
    cfg = SimpleNamespace(rungs={code: rung})
    return runners.claude_argv_tail(cfg, code, "prompt", "acceptEdits", None)


class TestEff1Clamp(unittest.TestCase):
    def test_default_sonnet_opus_efforts(self):
        cfg = config.from_dict(defaults())
        want = {"S": "high", "S+": "xhigh", "O": "high", "O+": "xhigh", "S+A": "xhigh"}
        for code, effort in want.items():
            self.assertEqual(cfg.rungs[code].effort, effort, code)
        self.assertEqual(cfg.rungs["S"].model, "claude-sonnet-5-5")
        self.assertEqual(cfg.rungs["S+"].model, "claude-sonnet-5-5")
        self.assertEqual(cfg.rungs["O"].model, "claude-opus-5-5")
        self.assertEqual(cfg.rungs["O+"].model, "claude-opus-5-5")
        self.assertEqual(cfg.rungs["S+A"].model, "claude-sonnet-5-5")
        self.assertEqual(cfg.rungs["S+A"].advisor, "claude-opus-5-5")
        for code, rung in cfg.rungs.items():
            matched = fnmatch.fnmatchcase(rung.model, "claude-sonnet-*") or fnmatch.fnmatchcase(rung.model, "claude-opus-*")
            if matched:
                self.assertIn(rung.effort, ("high", "xhigh"), code)
        self.assertIsNone(cfg.rungs["G-l"].effort)
        self.assertIsNone(cfg.rungs["C25"].effort)
        self.assertIsNone(cfg.rungs["K-h"].effort)
        self.assertEqual(cfg.judge["backends"]["haiku"]["effort"], "low")
        self.assertEqual(cfg.judge["backends"]["haiku"]["model"], "claude-haiku-5-5")

    def test_clamp_effort_table(self):
        fn = getattr(config, "clamp_effort", None)
        self.assertTrue(callable(fn))
        cases = (
            ("claude-sonnet-5-5", None, "high"),
            ("claude-sonnet-5-5", "low", "high"),
            ("claude-sonnet-5-5", "medium", "high"),
            ("claude-sonnet-5-5", "max", "xhigh"),
            ("claude-sonnet-5-5", "high", "high"),
            ("claude-sonnet-5-5", "xhigh", "xhigh"),
            ("claude-sonnet-9", "low", "high"),
            ("claude-opus-5-5", None, "high"),
            ("claude-opus-5-5", "low", "high"),
            ("claude-opus-5-5", "medium", "high"),
            ("claude-opus-5-5", "max", "xhigh"),
            ("claude-opus-5-5", "high", "high"),
            ("claude-opus-5-5", "xhigh", "xhigh"),
            ("claude-opus-4", "medium", "high"),
            ("grok-4.7-low-fast", None, None),
            ("grok-4.7-low-fast", "low", "low"),
            ("grok-4.7-medium-fast", "medium", "medium"),
            ("composer-2.5", None, None),
            ("composer-2.5", "max", "max"),
            ("kimi-k3-high", None, None),
            ("kimi-k3-max", "low", "low"),
            ("claude-haiku-5-5", None, None),
            ("claude-haiku-5-5", "low", "low"),
            ("claude-haiku-5-5", "max", "max"),
            ("claude-sonnet", "low", "low"),
            ("claude-opus", "max", "max"),
            ("Claude-Sonnet-5-5", "low", "low"),
            ("Claude-Opus-5-5", "medium", "medium"),
        )
        for model, effort, want in cases:
            self.assertEqual(fn(model, effort), want, (model, effort))

    def test_config_load_clamps_and_warns(self):
        raw = copy.deepcopy(defaults())
        raw["rungs"]["S"]["effort"] = "medium"
        raw["rungs"]["O+"]["effort"] = "max"
        del raw["rungs"]["O"]["effort"]
        cfg = config.from_dict(raw)
        self.assertEqual(cfg.rungs["S"].effort, "high")
        self.assertEqual(cfg.rungs["O+"].effort, "xhigh")
        self.assertEqual(cfg.rungs["O"].effort, "high")
        expected = (
            "rungs.S.effort: 'medium' raised to 'high' (claude-sonnet-5-5 runs only at high or xhigh)",
            "rungs.O+.effort: 'max' raised to 'xhigh' (claude-opus-5-5 runs only at high or xhigh)",
            "rungs.O.effort: None raised to 'high' (claude-opus-5-5 runs only at high or xhigh)",
        )
        for msg in expected:
            self.assertIn(msg, cfg.warnings)
        raw_ok = copy.deepcopy(defaults())
        raw_ok["rungs"]["S"]["effort"] = "high"
        raw_ok["rungs"]["S+"]["effort"] = "xhigh"
        raw_ok["rungs"]["O"]["effort"] = "high"
        raw_ok["rungs"]["O+"]["effort"] = "xhigh"
        cfg_ok = config.from_dict(raw_ok)
        warned = "\n".join(cfg_ok.warnings)
        for key in ("rungs.S.effort", "rungs.S+.effort", "rungs.O.effort", "rungs.O+.effort"):
            self.assertNotIn(key, warned)
        self.assertEqual(cfg_ok.rungs["S+"].effort, "xhigh")
        raw_other = copy.deepcopy(defaults())
        raw_other["rungs"]["G-l"]["effort"] = "low"
        raw_other["rungs"]["C25"]["effort"] = "medium"
        raw_other["rungs"]["K-h"]["effort"] = "max"
        cfg_other = config.from_dict(raw_other)
        self.assertEqual(cfg_other.rungs["G-l"].effort, "low")
        self.assertEqual(cfg_other.rungs["C25"].effort, "medium")
        self.assertEqual(cfg_other.rungs["K-h"].effort, "max")
        self.assertFalse(any("rungs.G-l.effort" in item for item in cfg_other.warnings))
        self.assertEqual(cfg_other.judge["backends"]["haiku"]["effort"], "low")
        raw_bad = copy.deepcopy(defaults())
        raw_bad["rungs"]["S"]["effort"] = "nope"
        with self.assertRaises(config.ConfigError) as caught:
            config.from_dict(raw_bad)
        self.assertEqual(str(caught.exception), "rungs.S.effort: must be low, medium, high, xhigh, or max")

    def test_argv_tail_clamps(self):
        cases = (
            ("claude-sonnet-5-5", "medium", "high"),
            ("claude-sonnet-5-5", None, "high"),
            ("claude-opus-5-5", "max", "xhigh"),
            ("claude-sonnet-5-5", "low", "high"),
            ("claude-opus-5-5", "high", "high"),
            ("claude-sonnet-5-5", "xhigh", "xhigh"),
            ("grok-4.7-low-fast", "medium", "medium"),
            ("grok-4.7-low-fast", None, None),
            ("composer-2.5", "low", "low"),
            ("kimi-k3-high", None, None),
            ("kimi-k3-max", "max", "max"),
            ("claude-haiku-5-5", "low", "low"),
            ("claude-haiku-5-5", None, None),
        )
        for model, effort, want in cases:
            self.assertEqual(_flag(_tail(model, effort)), want, (model, effort))

    def test_spa_argv_effort_and_advisor(self):
        cfg = config.from_dict(defaults())
        argv = runners.claude_argv_tail(cfg, "S+A", "do it", "acceptEdits", None)
        self.assertEqual(_flag(argv), "xhigh")
        self.assertEqual(argv[argv.index("--advisor") + 1], "claude-opus-5-5")
        self.assertEqual(argv.count("--effort"), 1)
        self.assertEqual(argv.count("--advisor"), 1)
        self.assertNotIn("--advisor-effort", argv)
        hand = _tail("claude-sonnet-5-5", "medium", "claude-opus-5-5", "S+A")
        self.assertEqual(_flag(hand), "high")
        self.assertEqual(hand[hand.index("--advisor") + 1], "claude-opus-5-5")
        self.assertEqual(hand.count("--effort"), 1)
        self.assertNotIn("--advisor-effort", hand)

    def test_planner_and_architect_from_rungs(self):
        cfg = config.from_dict(defaults())
        self.assertEqual(cfg.planner.rungs["hard"], "O")
        self.assertEqual(cfg.planner.rungs["critical"], "O")
        self.assertEqual(cfg.rungs["O"].model, "claude-opus-5-5")
        self.assertEqual(cfg.rungs["O"].effort, "high")
        architect = cfg.design_route["architect"]["rung"]
        self.assertEqual(architect, "O+")
        self.assertEqual(cfg.rungs[architect].model, "claude-opus-5-5")
        self.assertEqual(cfg.rungs[architect].effort, "xhigh")

    def test_pinned_tests_updated(self):
        cfg_test = _read(os.path.join(ROOT, "tests", "test_config.py"))
        self.assertIn('rung(cfg, "S").effort, "high"', cfg_test)
        self.assertNotIn('rung(cfg, "S").effort, "medium"', cfg_test)
        runners_test = _read(os.path.join(ROOT, "tests", "test_runners.py"))
        self.assertIn('("S", SONNET, "high")', runners_test)
        self.assertIn('("O+", OPUS, "xhigh")', runners_test)
        self.assertNotIn('("S", SONNET, "medium")', runners_test)
        advisor_test = _read(os.path.join(ROOT, "tests", "test_advisor.py"))
        self.assertIn('"--effort", "xhigh"', advisor_test)
        self.assertNotIn('"--effort", "high"', advisor_test)

    def test_new_effort_clamp_module(self):
        path = os.path.join(ROOT, "tests", "test_effort_clamp.py")
        self.assertTrue(os.path.isfile(path))
        text = _read(path)
        for needle in ("clamp_effort", "claude_argv_tail", "xhigh", "claude-haiku-5-5", "def test_"):
            self.assertIn(needle, text)
