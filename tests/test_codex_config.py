"""codex.config: the review.codex block, the CX reviewer rung and paths.codex (docs/codex/SPEC.md)."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import tempfile
import unittest

from mr import cli, config
from mr.config import ConfigError, from_dict
from mr.defaults import defaults
from tests.gitfix import make_repo


def _raw(**codex) -> dict:
    raw = copy.deepcopy(defaults())
    raw["review"]["codex"] = {**raw["review"].get("codex", {}), **codex}
    return raw


def _on() -> dict:
    return _raw(authors=["claude"])


# `mr route --json --no-models` on 330c5b6 (before codex.config): decision.ladder and roles per task
_MAIN_ROUTES = [
    (["fix the crash in src/a.ts"], ["G-m", "G-h"],
     {"judges": [{"check": "scope", "rung": "S", "when": "signal D has findings after H and A pass"}],
      "planner": None, "reviewer": None}),
    (["add a unit test for src/a.ts", "--difficulty", "medium"], ["G-h", "G-x"],
     {"judges": [{"check": "plan", "rung": "S", "when": "the plan is still invalid after a retry"},
                 {"check": "scope", "rung": "S", "when": "signal D has findings after H and A pass"}],
      "planner": "G-h", "reviewer": None}),
    (["refactor src/a.ts", "--type", "refactor", "--difficulty", "hard"], ["G-x", "S+", "O"],
     {"judges": [{"check": "plan", "rung": "S+", "when": "the plan is still invalid after a retry"},
                 {"check": "scope", "rung": "S+", "when": "signal D has findings after H and A pass"}],
      "planner": "O", "reviewer": "S"}),
    (["add login with oauth", "--type", "code", "--difficulty", "critical", "--route", "cascade"],
     ["G-x", "S+", "O"],
     {"judges": [{"check": "plan", "rung": "S+", "when": "the plan is still invalid after a retry"},
                 {"check": "scope", "rung": "S+", "when": "signal D has findings after H and A pass"}],
      "planner": "O", "reviewer": "S"}),
]


class TestCodexConfig(unittest.TestCase):
    def test_opt_in_loads_cx_reviewer(self):
        cfg = from_dict(_on())
        spec = config.reviewer_spec(cfg, "CX")
        self.assertEqual((spec.code, spec.path, spec.model, spec.effort), ("CX", "codex", "gpt-6.1-sol", "high"))
        self.assertEqual(cfg.review.codex.authors, frozenset({"claude"}))
        self.assertEqual(cfg.review.codex.recheck_s, 3600)
        self.assertIsNone(cfg.review.codex.calls_per_day)
        self.assertEqual(cfg.paths.codex, "codex")
        self.assertNotIn("CX", cfg.rungs)
        self.assertIs(config.reviewer_spec(cfg, "G-x"), cfg.rungs["G-x"])
        with self.assertRaises(ConfigError):
            config.reviewer_spec(cfg, "nope")

    def test_cx_cannot_be_named_elsewhere(self):
        mutations = {
            "ladder": lambda r: r["ladders"]["code"].__setitem__("easy", ["G-m", "CX"]),
            "planner": lambda r: r["planner"]["rungs"].__setitem__("easy", "CX"),
            "review.claude_denied": lambda r: r["review"].__setitem__("claude_denied", "CX"),
            "review.map": lambda r: r["review"]["map"].__setitem__("claude", "CX"),
            "rungs.CX": lambda r: r["rungs"].__setitem__("CX", {"model": "grok-4.7-high", "path": "cursor"}),
        }
        for name, mutate in mutations.items():
            with self.subTest(name):
                raw = _on()
                mutate(raw)
                with self.assertRaises(ConfigError):
                    from_dict(raw)

    def test_bad_codex_values_fail(self):
        bad = {
            "authors openai": {"authors": ["openai"]},
            "authors not a list": {"authors": "claude"},
            "effort ultra": {"authors": ["claude"], "effort": "ultra"},
            "model not gpt": {"authors": ["claude"], "model": "grok-4.7-high"},
            "model not allowed": {"authors": ["claude"], "model": "gpt-6.2"},
            "recheck_s zero": {"recheck_s": 0},
            "recheck_s bool": {"recheck_s": True},
            "calls_per_day zero": {"calls_per_day": 0},
        }
        for name, codex in bad.items():
            with self.subTest(name):
                with self.assertRaises(ConfigError):
                    from_dict(_raw(**codex))
        raw = _on()
        raw["paths"]["codex"] = "bin/codex"
        with self.assertRaises(ConfigError):
            from_dict(raw)

    def test_gpt_stays_out_of_rungs(self):
        raw = _on()
        raw["rungs"]["G-l"] = {"model": "gpt-6.1-sol", "path": "cursor"}
        with self.assertRaises(ConfigError):
            from_dict(raw)
        raw = _on()
        raw["allowed"].append("gpt-6.1-sol")
        with self.assertRaises(ConfigError):
            from_dict(raw)
        raw = _on()
        raw["allowed"].append("review:codex:grok-4.7-high")
        with self.assertRaises(ConfigError):
            from_dict(raw)

    def test_gpt_is_openai_family(self):
        from mr.review import family

        self.assertEqual(family("gpt-6.1-sol"), "openai")
        self.assertEqual(family("grok-4.7-xhigh"), "grok")

    def test_default_is_off_and_route_unchanged(self):
        cfg = config.load()
        self.assertEqual(cfg.review.codex.authors, frozenset())
        self.assertEqual(cfg.warnings, ())
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            make_repo(repo, {"src/a.ts": "export const a = 1\n"})
            for args, ladder, roles in _MAIN_ROUTES:
                with self.subTest(args[0]):
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        code = cli.main(["--json", "route", args[0], "--repo", repo, *args[1:], "--explain",
                                         "--no-models"])
                    self.assertEqual(code, 0, err.getvalue())
                    doc = json.loads(out.getvalue())
                    self.assertEqual(doc["decision"]["ladder"], ladder)
                    self.assertEqual(doc["roles"], roles)


if __name__ == "__main__":
    unittest.main()
