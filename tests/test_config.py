"""Configuration load and validation (spec §4, AC-CFG-1/3/4)."""
from __future__ import annotations

import copy
import os
import re
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

EXCLUDED = (
    ("gpt-*", "gpt-5"),
    ("gemini-*", "gemini-3-pro"),
    ("auto", "auto"),
    ("claude-haiku-*", "claude-haiku-5-5"),
    ("glm-*", "glm-5"),
    ("deepseek-*", "deepseek-v4"),
    ("claude-fable-*", "claude-fable-5-1"),
    ("muse-*", "muse-1"),
)



class TestConfig(unittest.TestCase):
    def test_example_valid(self):
        from mr.config import load, validate
        from mr.defaults import defaults

        raw = defaults()
        self.assertEqual(validate(raw), [])
        cfg = load()
        self.assertEqual(cfg.warnings, ())
        self.assertEqual(cfg.source, "<defaults>")
        self.assertEqual(cfg.version, 1)
        self.assertEqual(len(cfg.ladders) * len(next(iter(cfg.ladders.values()))), 50)
        from mr.config import ladder, rung

        self.assertEqual(ladder(cfg, "code", "trivial"), ("G-l", "G-m"))
        self.assertEqual(ladder(cfg, "code", "critical"), "design_route")
        self.assertEqual(rung(cfg, "S").path, "max")
        self.assertEqual(rung(cfg, "S").effort, "high")
        self.assertIsNone(rung(cfg, "G-l").effort)
        self.assertEqual(cfg.advisor["mode"], "on")  # advisor on by default

    def test_excluded_family_rejected(self):
        from mr.config import ConfigError, from_dict
        from mr.defaults import defaults

        for pattern, model in EXCLUDED:
            for bad in (pattern, model):
                raw = copy.deepcopy(defaults())
                raw["allowed"].append(bad)
                with self.assertRaises(ConfigError):
                    from_dict(raw)

                raw = copy.deepcopy(defaults())
                raw["rungs"]["G-l"]["model"] = bad
                with self.assertRaises(ConfigError):
                    from_dict(raw)

                raw = copy.deepcopy(defaults())
                raw["review"]["map"]["grok"] = bad
                with self.assertRaises(ConfigError):
                    from_dict(raw)

        raw = copy.deepcopy(defaults())
        raw["rungs"]["G-l"]["model"] = "composer-9"
        with self.assertRaises(ConfigError):
            from_dict(raw)

    def test_anthropic_cursor_rejected(self):
        # AC-CFG-5, hard rule §0.3: Anthropic models never on the cursor path.
        from mr.config import ConfigError, from_dict, is_anthropic, load
        from mr.defaults import defaults

        msg = "Anthropic models run only via the claude CLI (hard rule)"

        for model, want in (
            ("claude-sonnet-5-5", True),
            ("claude-opus-5-5-high", True),
            ("Claude-Haiku-5-5", True),
            ("sonnet", True),
            ("OPUS", True),
            ("haiku", True),
            ("grok-4.7-high", False),
            ("kimi-k3-max", False),
            ("composer-2.5", False),
            ("sonnetish", False),
        ):
            with self.subTest(model=model):
                self.assertIs(is_anthropic(model), want)

        def rejected(mutate):
            raw = copy.deepcopy(defaults())
            mutate(raw)
            with self.assertRaises(ConfigError) as caught:
                from_dict(raw)
            self.assertIn(msg, str(caught.exception))

        for entry in ("cursor:claude-sonnet-5-5-*", "claude-opus-5-5-high",
                      "cursor:claude-*", "cursor:sonnet", "opus"):
            with self.subTest(allowed=entry):
                rejected(lambda r, e=entry: r["allowed"].append(e))

        for model in ("claude-sonnet-5-5", "sonnet"):
            with self.subTest(rung_model=model):
                def mutate(r, m=model):
                    r["rungs"]["G-l"] = {"model": m, "path": "cursor"}
                    r["allowed"].append("cursor:" + m if m.startswith("claude-") else m)
                rejected(mutate)

        rejected(lambda r: r["ladders"]["code"].__setitem__("trivial", ["cursor:claude-sonnet-5-5"]))
        rejected(lambda r: r["fallback_claude_denied"].append("cursor:claude-opus-5-5"))
        rejected(lambda r: r["review"]["map"].__setitem__("grok", "cursor:claude-sonnet-5-5"))
        rejected(lambda r: r["classifier"].__setitem__("model", "claude-sonnet-5-5"))
        rejected(lambda r: r["design_route"]["architect_fallback"].append("cursor:claude-opus-5-5-high"))

        # max path and the role-scoped judge entry stay valid; defaults still load.
        raw = copy.deepcopy(defaults())
        raw["allowed"].append("max:claude-sonnet-5-5-*")
        from_dict(raw)
        cfg = load()
        self.assertIn("max:claude-sonnet-5-5", cfg.allowed)

    def test_canaries(self):
        from mr.config import ConfigError, from_dict, load
        from mr.defaults import defaults

        rejected = [
            "*",
            "cursor:*",
            "gpt-*",
            "cursor:gpt-5",
            "cursor:gemini-3-pro",
            "cursor:auto",
            "cursor:claude-haiku-5-5",
            "max:claude-haiku-5-5",
            "cursor:glm-5",
            "cursor:deepseek-v4",
            "max:claude-fable-5-1",
            "cursor:muse-1",
            "cursor:claude-sonnet-5-5-high",
            "cursor:claude-opus-5-5-high",
            "cursor:claude-sonnet-5-5",
            "claude-*",
            "cursor:claude*",
        ]
        for entry in rejected:
            raw = copy.deepcopy(defaults())
            raw["allowed"].append(entry)
            with self.assertRaises(ConfigError, msg=entry):
                from_dict(raw)

        raw = copy.deepcopy(defaults())
        raw["allowed"].append("judge:max:claude-haiku-5-5")
        from_dict(raw)

        for path in ("cursor", "max"):
            raw = copy.deepcopy(defaults())
            raw["rungs"]["G-l"] = {"model": "claude-haiku-5-5", "path": path}
            with self.assertRaises(ConfigError):
                from_dict(raw)

        for mutate in (
            lambda r: r["allowed"].append("cursor:claude-haiku-5-5"),
            lambda r: r["design_route"]["architect_fallback"].append("cursor:claude-haiku-5-5"),
            lambda r: r["hygiene"]["commands"].setdefault("fixture-repo", []).append("cursor:claude-haiku-5-5"),
        ):
            raw = copy.deepcopy(defaults())
            mutate(raw)
            with self.assertRaises(ConfigError):
                from_dict(raw)

        cfg = load(overrides={"ladders": {"explain": {"trivial": ["G-l", "G-m", "G-h", "G-x"]}}})
        self.assertIn("ladders.explain.trivial: 4 rungs > caps.attempts.trivial=3", cfg.warnings)
        cfg = load(overrides={"caps": {"attempts": {"critical": 1}}})
        self.assertIn("ladders.bugfix.critical: 2 rungs > caps.attempts.critical=1", cfg.warnings)
        self.assertFalse(any("ladders.code.critical" in w or "ladders.plan.hard" in w for w in cfg.warnings))

    def test_optional_blocks(self):
        from mr.config import from_dict
        from mr.defaults import defaults

        raw = copy.deepcopy(defaults())
        del raw["judge"]
        del raw["classifier"]
        raw.pop("advisor", None)
        cfg = from_dict(raw)
        self.assertIsNone(cfg.judge)
        self.assertIsNone(cfg.classifier)
        self.assertEqual(cfg.advisor["mode"], "on")  # advisor on by default

    def test_missing_ladder_cell(self):
        from mr.config import ConfigError, from_dict
        from mr.defaults import defaults

        raw = copy.deepcopy(defaults())
        del raw["ladders"]["code"]["medium"]
        with self.assertRaises(ConfigError) as caught:
            from_dict(raw)
        self.assertIn("ladders.code.medium", str(caught.exception))

    def test_undefined_rung(self):
        from mr.config import ConfigError, from_dict
        from mr.defaults import defaults

        raw = copy.deepcopy(defaults())
        raw["ladders"]["code"]["medium"] = ["G-h", "G-x", "Q"]
        with self.assertRaises(ConfigError) as caught:
            from_dict(raw)
        self.assertEqual(str(caught.exception), "ladders.code.medium[2]: rung 'Q' not defined")

    def test_explicit_missing_path(self):
        from mr.config import ConfigError, load

        missing = "/tmp/mr-no-such-routes.yaml"
        with self.assertRaises(ConfigError) as caught:
            load(missing)
        self.assertIn(missing, str(caught.exception))
        os.environ["MR_CONFIG"] = "/tmp/mr-no-such-env.yaml"
        try:
            with self.assertRaises(ConfigError) as caught:
                load()
            self.assertIn("/tmp/mr-no-such-env.yaml", str(caught.exception))
        finally:
            os.environ.pop("MR_CONFIG", None)

    def test_file_deep_merge(self):
        from mr.config import load

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "routes.yaml")
            Path(path).write_text(
                "caps:\n  attempts:\n    trivial: 5\n  claude_rungs_per_task: 9\n",
                encoding="utf-8",
            )
            cfg = load(path, overrides={"caps": {"attempts": {"trivial": 7}}})
        self.assertEqual(cfg.source, path)
        self.assertEqual(cfg.caps.attempts["trivial"], 7)
        self.assertEqual(cfg.caps.attempts["easy"], 3)
        self.assertEqual(cfg.caps.claude_rungs_per_task, 9)
        self.assertEqual(cfg.caps.opus_rungs_per_task, 1)
        self.assertEqual(cfg.rungs["G-l"].model, "grok-4.7-low")
        self.assertEqual(cfg.paths.claude, "claude")

    def test_paths_claude(self):
        from mr.config import ConfigError, from_dict
        from mr.defaults import defaults

        raw = copy.deepcopy(defaults())
        raw["paths"]["claude"] = "/tmp/not-the-wrapper"
        cfg = from_dict(raw)
        self.assertEqual(cfg.paths.claude, "/tmp/not-the-wrapper")
        self.assertEqual(cfg.paths.cursor_agent, "cursor-agent")

        raw["paths"]["check"] = "relative-check"
        with self.assertRaises(ConfigError) as caught:
            from_dict(raw)
        self.assertIn("paths.check", str(caught.exception))

        raw["paths"]["check"] = "/tmp/check.sh"
        os.environ["MR_TEST_ALLOW_FAKE_PATHS"] = "1"
        try:
            cfg = from_dict(raw)
        finally:
            os.environ.pop("MR_TEST_ALLOW_FAKE_PATHS", None)
        self.assertIn("MR_TEST_ALLOW_FAKE_PATHS set", cfg.warnings)
        self.assertEqual(cfg.paths.claude, "/tmp/not-the-wrapper")
        self.assertEqual(cfg.paths.cursor_agent, "cursor-agent")

    def test_cli_maps_config_error(self):
        import io
        import sys
        import types
        from contextlib import redirect_stderr

        from mr.config import ConfigError
        from mr.exitcodes import USAGE

        mod = types.ModuleType("mr.commands.route")

        def run(_args):
            raise ConfigError("ladders.code.medium[2]: rung 'Q' not defined")

        mod.run = run
        sys.modules["mr.commands.route"] = mod
        try:
            from mr.cli import main

            err = io.StringIO()
            with redirect_stderr(err):
                code = main(["route"])
        finally:
            sys.modules.pop("mr.commands.route", None)
        self.assertEqual(code, USAGE)
        self.assertEqual(err.getvalue(), "config error: ladders.code.medium[2]: rung 'Q' not defined\n")


def _codes(obj):
    """Every string leaf in a nested defaults block (rung codes, model names)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for val in obj.values():
            yield from _codes(val)
    elif isinstance(obj, (list, tuple)):
        for val in obj:
            yield from _codes(val)


class TestModelRoles(unittest.TestCase):  # model roles
    def test_no_default_ladder_or_map_uses_kimi(self):
        from mr.defaults import defaults

        raw = defaults()
        for key in ("ladders", "design_route", "judgment_route", "review", "planner",
                    "fallback_claude_denied", "escalation"):
            with self.subTest(block=key):
                bad = [c for c in _codes(raw[key]) if c.startswith("K-") or "kimi" in c]
                self.assertEqual(bad, [])
        self.assertIn("K-h", raw["rungs"])  # still defined for opt-in
        self.assertIn("K-x", raw["rungs"])

    def test_grok_rungs_default_non_fast(self):
        from mr.config import from_dict
        from mr.defaults import defaults

        cfg = from_dict(defaults())
        for code, effort in (("G-l", "low"), ("G-m", "medium"), ("G-h", "high"), ("G-x", "xhigh")):
            self.assertEqual(cfg.rungs[code].model, f"grok-4.7-{effort}")
            self.assertEqual(cfg.rungs[code].path, "cursor")

    def test_ladder_cells(self):
        from mr.config import from_dict
        from mr.defaults import defaults

        lad = from_dict(defaults()).ladders
        want = {
            ("code", "medium"): ("G-h", "G-x", "S"), ("code", "hard"): ("G-x", "S+", "O"),
            ("bugfix", "medium"): ("G-x", "S", "O"), ("refactor", "medium"): ("G-x", "S"),
            ("test", "medium"): ("G-h", "G-x"), ("test", "hard"): ("G-x", "S+"),
            ("review", "medium"): ("S",), ("review", "hard"): ("S",),
        }
        for (kind, diff), cell in want.items():
            self.assertEqual(lad[kind][diff], cell, (kind, diff))
        cfg = from_dict(defaults())
        self.assertEqual(cfg.design_route["implement"]["logic"], ["G-h", "G-x"])
        self.assertEqual(cfg.design_route["implement"]["native"], ["G-h", "G-x"])
        self.assertEqual(cfg.fallback_claude_denied, ("G-x",))

    def test_planner_and_review_blocks(self):
        from mr.config import from_dict
        from mr.defaults import defaults

        cfg = from_dict(defaults())
        self.assertEqual(cfg.planner.rungs, {"trivial": "G-h", "easy": "G-h", "medium": "G-h",
                                             "hard": "O", "critical": "O"})
        self.assertEqual(cfg.planner.claude_denied, "G-h")
        self.assertEqual(cfg.review.map, {"grok": "S", "kimi": "G-h", "composer": "G-h", "claude": "G-x"})
        self.assertEqual((cfg.review.high_risk_grok, cfg.review.tiebreak, cfg.review.claude_denied),
                         ("S", "S", "C25"))

    def test_planner_and_review_validation(self):
        from mr.config import ConfigError, from_dict
        from mr.defaults import defaults

        def rejected(mutate, needle):
            raw = copy.deepcopy(defaults())
            mutate(raw)
            with self.assertRaises(ConfigError) as caught:
                from_dict(raw)
            self.assertIn(needle, str(caught.exception))

        rejected(lambda r: r["planner"]["rungs"].pop("hard"), "planner.rungs.hard")
        rejected(lambda r: r["planner"]["rungs"].update(hard="Q"), "planner.rungs.hard")
        rejected(lambda r: r["planner"]["rungs"].update(bogus="G-h"), "planner.rungs.bogus")
        rejected(lambda r: r["planner"]["rungs"].update(hard="S+A"), "advisor rung")
        rejected(lambda r: r["planner"].update(claude_denied="O"), "planner.claude_denied")
        rejected(lambda r: r["planner"].update(claude_denied="Q"), "planner.claude_denied")
        rejected(lambda r: r.pop("planner"), "planner")
        rejected(lambda r: r["review"].update(claude_denied="S"), "review.claude_denied")
        rejected(lambda r: r["review"].update(claude_denied="Q"), "review.claude_denied")
        rejected(lambda r: r["review"].update(claude_denied="G-h"), "review.claude_denied")

    def test_old_config_with_cursor_paid_loads(self):
        from mr.config import load

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "routes.yaml")
            Path(path).write_text("cursor_paid: {max_rungs_per_day: 6}\n", encoding="utf-8")
            cfg = load(path)
        self.assertFalse(hasattr(cfg, "cursor_paid"))
        self.assertEqual(cfg.fallback_claude_denied, ("G-x",))

    def test_no_margin_in_defaults(self):
        from mr.defaults import defaults

        raw = defaults()
        self.assertNotIn("margin", raw["guard"])
        self.assertNotIn("limit", raw["guard"])
        self.assertNotIn("five_hour_max", raw["guard"])
        self.assertNotIn("yield_at_hour_usage", raw["judge"]["budget"])
        self.assertNotIn("yield_at_five_hour_pct", raw["judge"]["budget"])
        self.assertEqual(raw["claude_limits"], {"hour_max": None, "five_hour_max": 85})

    def test_legacy_limits_map(self):
        from mr.config import ConfigError, load

        cfg = load(overrides={"guard": {"limit": 40, "margin": 5, "five_hour_max": 92}})
        self.assertEqual((cfg.claude_limits.hour_max, cfg.claude_limits.five_hour_max), (35, 92))
        self.assertEqual(cfg.warnings, ())

        # limit alone does not assume the old margin of 5
        cfg = load(overrides={"guard": {"limit": 40}})
        self.assertEqual((cfg.claude_limits.hour_max, cfg.claude_limits.five_hour_max), (40, 85))

        cfg = load(overrides={"judge": {"budget": {
            "yield_at_hour_usage": 22, "yield_at_five_hour_pct": 70}}})
        self.assertEqual((cfg.claude_limits.hour_max, cfg.claude_limits.five_hour_max), (22, 70))
        self.assertEqual(cfg.warnings, ())

        cfg = load(overrides={
            "guard": {"limit": 40, "margin": 5, "five_hour_max": 92},
            "judge": {"budget": {"yield_at_hour_usage": 10, "yield_at_five_hour_pct": 50}},
        })
        self.assertEqual((cfg.claude_limits.hour_max, cfg.claude_limits.five_hour_max), (35, 92))
        self.assertTrue(any("guard values win" in w for w in cfg.warnings))

        cfg = load(overrides={
            "claude_limits": {"hour_max": 30, "five_hour_max": 85},
            "guard": {"limit": 40, "margin": 5, "five_hour_max": 92},
        })
        self.assertEqual((cfg.claude_limits.hour_max, cfg.claude_limits.five_hour_max), (30, 85))
        self.assertTrue(any("claude_limits wins" in w for w in cfg.warnings))

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "routes.yaml")
            Path(path).write_text(
                "guard: {limit: 40, margin: 5, five_hour_max: 92}\n", encoding="utf-8")
            cfg = load(path)
        self.assertEqual((cfg.claude_limits.hour_max, cfg.claude_limits.five_hour_max), (35, 92))
        self.assertEqual(cfg.warnings, ())

        with self.assertRaises(ConfigError):
            load(overrides={"claude_limits": {"hour_max": 30, "five_hour_max": 101}})

        # legacy mappings that are not legal claude_limits values keep the default
        cfg = load(overrides={"guard": {"limit": 5, "margin": 5}})
        self.assertEqual((cfg.claude_limits.hour_max, cfg.claude_limits.five_hour_max), (None, 85))
        self.assertTrue(any("keeping default" in w for w in cfg.warnings))

        cfg = load(overrides={"guard": {"limit": 5, "margin": 5, "five_hour_max": 92}})
        self.assertEqual((cfg.claude_limits.hour_max, cfg.claude_limits.five_hour_max), (None, 92))

        cfg = load(overrides={"guard": {"five_hour_max": 101}})
        self.assertEqual(cfg.claude_limits.five_hour_max, 85)
        self.assertTrue(any("keeping default" in w for w in cfg.warnings))

        cfg = load(overrides={"judge": {"budget": {"yield_at_hour_usage": 0, "yield_at_five_hour_pct": -1}}})
        self.assertEqual((cfg.claude_limits.hour_max, cfg.claude_limits.five_hour_max), (None, 85))
        self.assertTrue(any("keeping default" in w for w in cfg.warnings))

        # a partial claude_limits block warns only for keys the user set
        cfg = load(overrides={
            "claude_limits": {"hour_max": 28},
            "guard": {"five_hour_max": 92},
        })
        self.assertEqual((cfg.claude_limits.hour_max, cfg.claude_limits.five_hour_max), (28, 85))
        self.assertFalse(any("claude_limits wins" in w for w in cfg.warnings))

        cfg = load(overrides={
            "claude_limits": {"hour_max": 28},
            "guard": {"limit": 40, "margin": 5},
        })
        self.assertEqual(cfg.claude_limits.hour_max, 28)
        self.assertTrue(any("claude_limits wins" in w for w in cfg.warnings))


if __name__ == "__main__":
    unittest.main()
