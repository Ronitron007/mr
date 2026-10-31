"""EFF2 locked checks: default Cursor Grok rungs drop -fast, and S+A effort follows clamp_effort."""
from __future__ import annotations

import os
import re
import unittest
from types import SimpleNamespace

from mr import config, runners
from mr.defaults import DEFAULT_YAML, defaults

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAST = re.compile(r"grok-4\.7-(low|medium|high|xhigh)-fast")
NONFAST = {
    "G-l": "grok-4.7-low",
    "G-m": "grok-4.7-medium",
    "G-h": "grok-4.7-high",
    "G-x": "grok-4.7-xhigh",
}
PIN_FILES = (
    "tests/test_config.py",
    "tests/test_planner.py",
    "tests/test_review.py",
    "tests/test_loop_run.py",
    "tests/test_run_once.py",
    "tests/fixtures/advisor/decide_table.json",
    "tests/fixtures/p0/failure_table.json",
)


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


def _tail(model, effort, advisor=None, code="S+A"):
    rung = config.Rung(code, model, "max", effort, advisor)
    cfg = SimpleNamespace(rungs={code: rung})
    return runners.claude_argv_tail(cfg, code, "prompt", "acceptEdits", None)


class TestEff2GrokSlugs(unittest.TestCase):
    def test_default_grok_rungs_non_fast(self):
        cfg = config.from_dict(defaults())
        for code, model in NONFAST.items():
            rung = cfg.rungs[code]
            self.assertEqual(rung.model, model, code)
            self.assertNotIn("-fast", rung.model)
            self.assertEqual(rung.path, "cursor")
            self.assertRegex(rung.model, r"^grok-4\.7-(low|medium|high|xhigh)$")
        self.assertEqual(cfg.classifier["model"], "grok-4.7-low")
        bad = []
        pkg = os.path.join(ROOT, "mr")
        for dirpath, _dirs, names in os.walk(pkg):
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                if FAST.search(_read(path)):
                    bad.append(os.path.relpath(path, ROOT))
        self.assertEqual(bad, [])

    def test_planner_and_fallback_models(self):
        cfg = config.from_dict(defaults())
        for diff in ("trivial", "easy", "medium"):
            code = cfg.planner.rungs[diff]
            self.assertEqual(code, "G-h", diff)
            self.assertEqual(cfg.rungs[code].model, "grok-4.7-high", diff)
        self.assertEqual(cfg.planner.claude_denied, "G-h")
        self.assertEqual(cfg.rungs[cfg.planner.claude_denied].model, "grok-4.7-high")
        self.assertEqual(cfg.fallback_claude_denied, ("G-x",))
        self.assertEqual(cfg.rungs["G-x"].model, "grok-4.7-xhigh")

    def test_default_slug_fixtures_and_pins(self):
        for rel in PIN_FILES:
            text = _read(os.path.join(ROOT, rel))
            self.assertIsNone(FAST.search(text), rel)
        config_test = _read(os.path.join(ROOT, "tests", "test_config.py"))
        self.assertNotIn("-fast", config_test)
        classify = _read(os.path.join(ROOT, "tests", "test_classify.py"))
        self.assertIn("grok-4.7-high-fast", classify)
        self.assertIn("grok-4.7-low-fast", classify)

    def test_spa_effort_is_clamp(self):
        cases = (
            ("claude-sonnet-5-5", "high", "claude-opus-5-5", "S+A"),
            ("claude-sonnet-5-5", "medium", "claude-opus-5-5", "S+A"),
            ("claude-sonnet-5-5", "low", "claude-opus-5-5", "S+A"),
            ("claude-sonnet-5-5", "xhigh", "claude-opus-5-5", "S+A"),
            ("claude-sonnet-5-5", "medium", None, "S"),
            ("claude-opus-5-5", "max", None, "O+"),
        )
        for model, effort, advisor, code in cases:
            argv = _tail(model, effort, advisor, code)
            want = config.clamp_effort(model, effort)
            self.assertEqual(_flag(argv), want, (model, effort, advisor))
            self.assertNotIn("--advisor-effort", argv)
        cfg = config.from_dict(defaults())
        for code, effort in (("S", "high"), ("S+", "xhigh"), ("O", "high"), ("O+", "xhigh"), ("S+A", "xhigh")):
            self.assertEqual(cfg.rungs[code].effort, effort, code)
        argv = runners.claude_argv_tail(cfg, "S+A", "do it", "acceptEdits", None)
        self.assertEqual(_flag(argv), "xhigh")
        self.assertEqual(argv[argv.index("--advisor") + 1], "claude-opus-5-5")
        src = _read(os.path.join(ROOT, "mr", "runners.py"))
        self.assertNotIn('if adv and config.clamp_effort(r.model, "low") == "high"', src)
        self.assertNotIn('eff = "xhigh"', src)
        effort_test = _read(os.path.join(ROOT, "tests", "test_effort_clamp.py"))
        eff1 = _read(os.path.join(ROOT, "tests", "test_eff1_clamp_mr.py"))
        self.assertNotIn('_flag(spa), "xhigh"', effort_test)
        self.assertNotIn('_flag(hand), "xhigh"', eff1)
        self.assertIn("grok-4.7-low-fast", effort_test)
        self.assertIn("grok-4.7-low-fast", eff1)
        self.assertIn("grok-4.7-high", effort_test)
        self.assertIn("grok-4.7-xhigh", effort_test)


if __name__ == "__main__":
    unittest.main()
