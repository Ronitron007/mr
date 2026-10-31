"""Sonnet and Opus efforts are only high or xhigh (EFF1)."""
from __future__ import annotations

import copy
import fnmatch
import unittest
from types import SimpleNamespace

from mr import config, runners
from mr.defaults import defaults


def _flag(argv):
    if "--effort" not in argv:
        return None
    return argv[argv.index("--effort") + 1]


def _tail(model, effort, advisor=None, code="X"):
    rung = config.Rung(code, model, "max", effort, advisor)
    cfg = SimpleNamespace(rungs={code: rung})
    return runners.claude_argv_tail(cfg, code, "prompt", "acceptEdits", None)


class TestEffortClamp(unittest.TestCase):
    def test_default_rung_efforts(self):
        cfg = config.from_dict(defaults())
        want = {"S": "high", "S+": "xhigh", "O": "high", "O+": "xhigh", "S+A": "xhigh"}
        for code, effort in want.items():
            self.assertEqual(cfg.rungs[code].effort, effort, code)
        self.assertEqual(cfg.rungs["S+A"].advisor, "claude-opus-5-5")
        for code, rung in cfg.rungs.items():
            matched = fnmatch.fnmatchcase(rung.model, "claude-sonnet-*") or fnmatch.fnmatchcase(
                rung.model, "claude-opus-*"
            )
            if matched:
                self.assertIn(rung.effort, ("high", "xhigh"), code)
        self.assertIsNone(cfg.rungs["G-l"].effort)
        self.assertEqual(cfg.rungs["G-h"].model, "grok-4.7-high")
        self.assertEqual(cfg.rungs["G-x"].model, "grok-4.7-xhigh")
        for code, effort in (("G-l", "low"), ("G-m", "medium"), ("G-h", "high"), ("G-x", "xhigh")):
            self.assertEqual(cfg.rungs[code].model, f"grok-4.7-{effort}")
            self.assertNotIn("-fast", cfg.rungs[code].model)
            self.assertEqual(cfg.rungs[code].path, "cursor")
        for diff in ("trivial", "easy", "medium"):
            self.assertEqual(cfg.planner.rungs[diff], "G-h")
            self.assertEqual(cfg.rungs[cfg.planner.rungs[diff]].model, "grok-4.7-high")
        self.assertIsNone(cfg.rungs["C25"].effort)
        self.assertIsNone(cfg.rungs["K-h"].effort)
        self.assertEqual(cfg.judge["backends"]["haiku"]["model"], "claude-haiku-5-5")
        self.assertEqual(cfg.judge["backends"]["haiku"]["effort"], "low")

    def test_clamp_effort_table(self):
        fn = config.clamp_effort
        cases = (
            ("claude-sonnet-5-5", None, "high"),
            ("claude-sonnet-5-5", "low", "high"),
            ("claude-sonnet-5-5", "medium", "high"),
            ("claude-sonnet-5-5", "max", "xhigh"),
            ("claude-sonnet-5-5", "high", "high"),
            ("claude-sonnet-5-5", "xhigh", "xhigh"),
            ("claude-sonnet-5-5", "nope", "high"),
            ("claude-opus-5-5", None, "high"),
            ("claude-opus-5-5", "low", "high"),
            ("claude-opus-5-5", "medium", "high"),
            ("claude-opus-5-5", "max", "xhigh"),
            ("claude-opus-5-5", "high", "high"),
            ("claude-opus-5-5", "xhigh", "xhigh"),
            ("grok-4.7-low-fast", None, None),
            ("grok-4.7-low-fast", "low", "low"),
            ("composer-2.5", None, None),
            ("kimi-k3-high", None, None),
            ("kimi-k3-high", "low", "low"),
            ("claude-haiku-5-5", None, None),
            ("claude-haiku-5-5", "low", "low"),
            ("claude-sonnet", "low", "low"),
            ("claude-opus", "max", "max"),
            ("Claude-Sonnet-5-5", "low", "low"),
        )
        for model, effort, want in cases:
            self.assertEqual(fn(model, effort), want, (model, effort))

    def test_config_override_warnings(self):
        raw = copy.deepcopy(defaults())
        raw["rungs"]["S"]["effort"] = "medium"
        raw["rungs"]["O+"]["effort"] = "max"
        del raw["rungs"]["O"]["effort"]
        cfg = config.from_dict(raw)
        self.assertEqual(cfg.rungs["S"].effort, "high")
        self.assertEqual(cfg.rungs["O+"].effort, "xhigh")
        self.assertEqual(cfg.rungs["O"].effort, "high")
        self.assertIn(
            "rungs.S.effort: 'medium' raised to 'high' (claude-sonnet-5-5 runs only at high or xhigh)",
            cfg.warnings,
        )
        self.assertIn(
            "rungs.O+.effort: 'max' raised to 'xhigh' (claude-opus-5-5 runs only at high or xhigh)",
            cfg.warnings,
        )
        self.assertIn(
            "rungs.O.effort: None raised to 'high' (claude-opus-5-5 runs only at high or xhigh)",
            cfg.warnings,
        )
        raw_ok = copy.deepcopy(defaults())
        raw_ok["rungs"]["S"]["effort"] = "high"
        raw_ok["rungs"]["O+"]["effort"] = "xhigh"
        cfg_ok = config.from_dict(raw_ok)
        warned = "\n".join(cfg_ok.warnings)
        self.assertNotIn("rungs.S.effort", warned)
        self.assertNotIn("rungs.O+.effort", warned)
        raw_bad = copy.deepcopy(defaults())
        raw_bad["rungs"]["S"]["effort"] = "nope"
        with self.assertRaises(config.ConfigError) as caught:
            config.from_dict(raw_bad)
        self.assertEqual(str(caught.exception), "rungs.S.effort: must be low, medium, high, xhigh, or max")

    def test_hand_built_argv_guard(self):
        cases = (
            ("claude-sonnet-5-5", "medium", "high"),
            ("claude-sonnet-5-5", None, "high"),
            ("claude-sonnet-5-5", "low", "high"),
            ("claude-opus-5-5", "max", "xhigh"),
            ("grok-4.7-low-fast", None, None),
            ("composer-2.5", "low", "low"),
            ("kimi-k3-high", None, None),
            ("claude-haiku-5-5", "low", "low"),
            ("claude-haiku-5-5", None, None),
        )
        for model, effort, want in cases:
            self.assertEqual(_flag(_tail(model, effort)), want, (model, effort))
        spa = _tail("claude-sonnet-5-5", "medium", "claude-opus-5-5", "S+A")
        self.assertEqual(_flag(spa), "high")
        self.assertEqual(spa.count("--effort"), 1)
        self.assertEqual(spa[spa.index("--advisor") + 1], "claude-opus-5-5")
        self.assertEqual(spa.count("--advisor"), 1)
        self.assertNotIn("--advisor-effort", spa)
        for effort in ("high", "low"):
            pinned = _tail("claude-sonnet-5-5", effort, "claude-opus-5-5", "S+A")
            self.assertEqual(_flag(pinned), "high", effort)
            self.assertNotIn("--advisor-effort", pinned)

    def test_planner_and_architect_efforts(self):
        cfg = config.from_dict(defaults())
        self.assertEqual(cfg.planner.rungs["hard"], "O")
        self.assertEqual(cfg.planner.rungs["critical"], "O")
        self.assertEqual(cfg.rungs["O"].model, "claude-opus-5-5")
        self.assertEqual(cfg.rungs["O"].effort, "high")
        architect = cfg.design_route["architect"]["rung"]
        self.assertEqual(architect, "O+")
        self.assertNotIn("effort", cfg.design_route["architect"])
        self.assertEqual(cfg.rungs[architect].model, "claude-opus-5-5")
        self.assertEqual(cfg.rungs[architect].effort, "xhigh")
        argv = runners.claude_argv_tail(cfg, "S+A", "do it", "acceptEdits", None)
        self.assertEqual(_flag(argv), "xhigh")
        self.assertEqual(argv.count("--advisor"), 1)
