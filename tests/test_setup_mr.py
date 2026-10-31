"""mr setup writes a user routes.yaml from detected provider CLIs."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from mr import cli, exitcodes
from mr.config import load

CLAUDE_RUNGS = ("S", "S+", "O", "O+", "S+A")
CURSOR_RUNGS = ("G-l", "G-m", "G-h", "G-x", "C25", "K-h", "K-x")
CURSOR_HINT = "install Cursor CLI, then connect it: cursor-agent status"
CLAUDE_HINT = "install Claude Code, then run claude once to connect it"


class SetupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.saved = {}
        for key in ("PATH", "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "MR_HOME", "MR_CONFIG"):
            self.saved[key] = os.environ.get(key)
        os.environ["PATH"] = str(self.bin)
        os.environ["HOME"] = str(self.root / "home")
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "xdg")
        os.environ["MR_HOME"] = str(self.root / "mrhome")
        os.environ.pop("MR_CONFIG", None)
        os.environ.pop("XDG_DATA_HOME", None)
        Path(os.environ["HOME"]).mkdir()
        Path(os.environ["MR_HOME"]).mkdir()

    def tearDown(self):
        for key, val in self.saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self._tmp.cleanup()

    def exe(self, name):
        path = self.bin / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_mr(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def mod(self):
        try:
            from mr import providers
        except ImportError:
            self.fail("mr.providers is not importable")
        return providers

    def ladder_codes(self, cfg):
        codes = []
        for table in cfg.ladders.values():
            for cell in table.values():
                if cell == "design_route":
                    continue
                codes.extend(cell)
        return codes

    def test_help_lists_setup(self):
        code, out, err = self.run_mr(["--help"])
        self.assertEqual(code, exitcodes.OK)
        self.assertIn("setup", out)

    def test_setup_help_flags(self):
        code, out, err = self.run_mr(["setup", "--help"])
        self.assertEqual(code, exitcodes.OK, err)
        for flag in ("--provider", "--cursor-agent", "--claude", "--yes", "--force", "--print", "--check"):
            self.assertIn(flag, out)

    def test_bad_provider_choice(self):
        code, out, err = self.run_mr(["setup", "--provider", "gpt", "--yes"])
        self.assertEqual(code, exitcodes.USAGE)
        self.assertIn("gpt", err)

    def test_detect_registry_and_overrides(self):
        providers = self.mod()
        self.assertEqual(providers.PROVIDERS["cursor"]["binary"], "cursor-agent")
        self.assertEqual(providers.PROVIDERS["cursor"]["config_key"], "cursor_agent")
        self.assertEqual(providers.PROVIDERS["cursor"]["rung_path"], "cursor")
        self.assertEqual(providers.PROVIDERS["cursor"]["hint"], CURSOR_HINT)
        self.assertEqual(providers.PROVIDERS["claude"]["binary"], "claude")
        self.assertEqual(providers.PROVIDERS["claude"]["config_key"], "claude")
        self.assertEqual(providers.PROVIDERS["claude"]["rung_path"], "max")
        self.assertEqual(providers.PROVIDERS["claude"]["hint"], CLAUDE_HINT)
        cursor = self.exe("cursor-agent")
        found = providers.detect()
        self.assertEqual(found["cursor"], str(cursor))
        self.assertIsNone(found["claude"])
        claude = self.root / "claude-bin"
        claude.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        claude.chmod(0o755)
        found = providers.detect({"claude": str(claude)})
        self.assertEqual(found["claude"], str(claude))
        self.assertEqual(found["cursor"], str(cursor))
        plain = self.root / "not-exec"
        plain.write_text("x", encoding="utf-8")
        found = providers.detect({"cursor": str(plain), "claude": str(self.root / "missing")})
        self.assertIsNone(found["cursor"])
        self.assertIsNone(found["claude"])

    def test_rung_map(self):
        rung_map = self.mod().rung_map
        self.assertEqual(rung_map(["cursor", "claude"]), {})
        claude_off = rung_map(["cursor"])
        for code in CLAUDE_RUNGS:
            self.assertEqual(claude_off[code], "G-x")
        for code in CURSOR_RUNGS:
            self.assertNotIn(code, claude_off)
        cursor_off = rung_map(["claude"])
        for code in ("G-l", "G-m", "G-h", "C25", "K-h", "K-x"):
            self.assertEqual(cursor_off[code], "S")
        self.assertEqual(cursor_off["G-x"], "S+")
        for code in CLAUDE_RUNGS:
            self.assertNotIn(code, cursor_off)

    def test_build_config_validates(self):
        providers = self.mod()
        cursor = str(self.exe("cursor-agent"))
        overlay = providers.build_config(["cursor"], {"cursor": cursor, "claude": None})
        self.assertEqual(overlay["version"], 1)
        self.assertEqual(overlay["paths"]["cursor_agent"], cursor)
        self.assertNotIn("claude", overlay["paths"])
        self.assertEqual(overlay["advisor"], {"mode": "off"})
        target = self.root / "from-build.yaml"
        target.write_text(yaml.safe_dump(overlay), encoding="utf-8")
        cfg = load(str(target))
        self.assertEqual(cfg.ladders["review"]["medium"], ("G-x",))
        self.assertEqual(cfg.advisor["mode"], "off")
        both = providers.build_config(
            ["claude", "cursor"],
            {"cursor": cursor, "claude": str(self.exe("claude"))},
        )
        self.assertEqual(set(both), {"version", "paths"})
        self.assertEqual(both["paths"]["cursor_agent"], cursor)
        self.assertTrue(both["paths"]["claude"].endswith("claude"))

    def test_cursor_only_load_and_ladders(self):
        cursor = self.exe("cursor-agent")
        target = self.root / "routes.yaml"
        code, out, err = self.run_mr(["--config", str(target), "setup", "--yes", "--provider", "cursor"])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertTrue(target.is_file())
        self.assertIn(str(target), out)
        self.assertIn("mr route --explain", out)
        self.assertIn("mr run", out)
        body = target.read_text(encoding="utf-8").lower()
        for banned in ("api_key", "secret", "password", "credential"):
            self.assertNotIn(banned, body)
        cfg = load(str(target))
        self.assertEqual(cfg.paths.cursor_agent, str(cursor))
        self.assertEqual(cfg.advisor["mode"], "off")
        for rung in CLAUDE_RUNGS:
            self.assertNotIn(rung, self.ladder_codes(cfg))
        self.assertEqual(cfg.ladders["code"]["trivial"], ("G-l", "G-m"))
        self.assertEqual(cfg.ladders["code"]["easy"], ("G-m", "G-h", "G-x"))
        self.assertEqual(cfg.ladders["code"]["medium"], ("G-h", "G-x"))
        self.assertEqual(cfg.ladders["code"]["hard"], ("G-x",))
        self.assertEqual(cfg.ladders["code"]["critical"], "design_route")
        self.assertEqual(cfg.ladders["bugfix"]["critical"], ("G-x",))
        self.assertEqual(cfg.design_route["architect"]["rung"], "G-x")
        self.assertEqual(list(cfg.design_route["architect_fallback"]), ["G-x", "G-x+human_spec_review", "defer"])
        self.assertEqual(list(cfg.design_route["implement"]["mechanical"]), ["C25", "G-h", "G-x"])
        self.assertEqual(list(cfg.design_route["implement"]["then"]), ["G-x"])
        self.assertEqual(list(cfg.judgment_route["implement"]), ["G-x"])
        self.assertEqual(list(cfg.judgment_route["critical"]), ["G-x"])
        self.assertEqual(cfg.fallback_claude_denied, ("G-x",))
        self.assertEqual(cfg.review.map["grok"], "G-x")
        self.assertEqual(cfg.review.high_risk_grok, "G-x")
        self.assertEqual(cfg.review.tiebreak, "G-x")
        self.assertEqual(cfg.review.claude_denied, "C25")
        self.assertEqual(cfg.planner.rungs["hard"], "G-x")
        self.assertEqual(cfg.planner.rungs["critical"], "G-x")
        self.assertEqual(cfg.planner.claude_denied, "G-h")

    def test_claude_only_load_and_ladders(self):
        claude = self.exe("claude")
        target = self.root / "routes.yaml"
        code, out, err = self.run_mr(["--config", str(target), "setup", "--yes", "--provider", "claude"])
        self.assertEqual(code, exitcodes.OK, err)
        cfg = load(str(target))
        self.assertEqual(cfg.paths.claude, str(claude))
        self.assertEqual(cfg.advisor["mode"], "on")
        for rung in CURSOR_RUNGS:
            self.assertNotIn(rung, self.ladder_codes(cfg))
        self.assertEqual(cfg.ladders["code"]["trivial"], ("S",))
        self.assertEqual(cfg.ladders["code"]["medium"], ("S", "S+", "S"))
        self.assertEqual(cfg.ladders["code"]["hard"], ("S+", "O"))
        self.assertEqual(cfg.ladders["code"]["critical"], "design_route")
        self.assertEqual(cfg.ladders["explain"]["hard"], ("S+", "S"))
        self.assertEqual(cfg.design_route["architect"]["rung"], "O+")
        self.assertEqual(list(cfg.design_route["architect_fallback"]), ["S+", "G-x+human_spec_review", "defer"])
        self.assertEqual(list(cfg.design_route["implement"]["mechanical"]), ["S", "S+"])
        self.assertEqual(list(cfg.design_route["implement"]["logic"]), ["S", "S+"])
        self.assertEqual(list(cfg.judgment_route["implement"]), ["S+"])
        self.assertEqual(list(cfg.judgment_route["critical"]), ["O"])
        self.assertEqual(cfg.fallback_claude_denied, ("S+",))
        self.assertEqual(cfg.review.map["kimi"], "S")
        self.assertEqual(cfg.review.map["claude"], "S+")
        self.assertEqual(cfg.review.high_risk_grok, "S")
        self.assertEqual(cfg.review.tiebreak, "S")
        self.assertEqual(cfg.review.claude_denied, "C25")
        self.assertEqual(cfg.planner.rungs["trivial"], "S")
        self.assertEqual(cfg.planner.rungs["medium"], "S")
        self.assertEqual(cfg.planner.rungs["hard"], "O")
        self.assertEqual(cfg.planner.claude_denied, "G-h")

    def test_both_load_without_rewrite(self):
        cursor = self.exe("cursor-agent")
        claude = self.exe("claude")
        target = self.root / "routes.yaml"
        code, out, err = self.run_mr(["--config", str(target), "setup", "--yes"])
        self.assertEqual(code, exitcodes.OK, err)
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        self.assertEqual(set(raw), {"version", "paths"})
        self.assertEqual(raw["paths"]["cursor_agent"], str(cursor))
        self.assertEqual(raw["paths"]["claude"], str(claude))
        cfg = load(str(target))
        self.assertEqual(cfg.ladders["code"]["easy"], ("G-m", "G-h", "S"))
        self.assertEqual(cfg.ladders["code"]["critical"], "design_route")
        self.assertEqual(cfg.advisor["mode"], "on")
        self.assertEqual(cfg.design_route["architect"]["rung"], "O+")

    def test_auto_only_cursor_rewrites(self):
        cursor = self.exe("cursor-agent")
        target = self.root / "routes.yaml"
        with mock.patch("sys.stdin.isatty", return_value=False):
            code, out, err = self.run_mr(["--config", str(target), "setup"])
        self.assertEqual(code, exitcodes.OK, err)
        cfg = load(str(target))
        self.assertEqual(cfg.paths.cursor_agent, str(cursor))
        self.assertEqual(cfg.advisor["mode"], "off")
        for rung in CLAUDE_RUNGS:
            self.assertNotIn(rung, self.ladder_codes(cfg))

    def test_notty_enables_all_detected(self):
        cursor = self.exe("cursor-agent")
        claude = self.exe("claude")
        target = self.root / "routes.yaml"
        with mock.patch("sys.stdin.isatty", return_value=False):
            code, out, err = self.run_mr(["--config", str(target), "setup"])
        self.assertEqual(code, exitcodes.OK, err)
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        self.assertEqual(set(raw), {"version", "paths"})
        self.assertEqual(raw["paths"]["cursor_agent"], str(cursor))
        self.assertEqual(raw["paths"]["claude"], str(claude))

    def test_existing_file_not_overwritten(self):
        self.exe("cursor-agent")
        self.exe("claude")
        target = self.root / "routes.yaml"
        original = "version: 1\nkept: true\n"
        target.write_text(original, encoding="utf-8")
        code, out, err = self.run_mr(["--config", str(target), "setup", "--yes"])
        self.assertEqual(code, exitcodes.USAGE, err)
        self.assertIn(str(target), err)
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_force_overwrites(self):
        cursor = self.exe("cursor-agent")
        target = self.root / "routes.yaml"
        target.write_text("kept: true\n", encoding="utf-8")
        code, out, err = self.run_mr(["--config", str(target), "setup", "--yes", "--force", "--provider", "cursor"])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertNotIn("kept:", target.read_text(encoding="utf-8"))
        self.assertEqual(load(str(target)).paths.cursor_agent, str(cursor))

    def test_print_writes_nothing(self):
        cursor = self.exe("cursor-agent")
        target = self.root / "nested" / "routes.yaml"
        code, out, err = self.run_mr(["--config", str(target), "setup", "--print", "--yes", "--provider", "cursor"])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertFalse((self.root / "nested").exists())
        printed = self.root / "printed.yaml"
        printed.write_text(out, encoding="utf-8")
        cfg = load(str(printed))
        self.assertEqual(cfg.paths.cursor_agent, str(cursor))
        self.assertEqual(cfg.advisor["mode"], "off")
        self.assertEqual(cfg.ladders["code"]["medium"], ("G-h", "G-x"))

    def test_print_json_writes_nothing(self):
        self.exe("claude")
        target = self.root / "routes.yaml"
        code, out, err = self.run_mr(["--json", "--config", str(target), "setup", "--print", "--yes", "--provider", "claude"])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertFalse(target.exists())
        payload = json.loads(out)
        self.assertEqual(set(payload), {"path", "providers", "written"})
        self.assertEqual(payload["path"], str(target))
        self.assertEqual(payload["providers"], ["claude"])
        self.assertIs(payload["written"], False)

    def test_json_and_success_text(self):
        self.exe("cursor-agent")
        self.exe("claude")
        target = self.root / "routes.yaml"
        code, out, err = self.run_mr(["--json", "--config", str(target), "setup", "--yes"])
        self.assertEqual(code, exitcodes.OK, err)
        payload = json.loads(out)
        self.assertEqual(set(payload), {"path", "providers", "written"})
        self.assertEqual(payload["path"], str(target))
        self.assertEqual(payload["providers"], ["cursor", "claude"])
        self.assertIs(payload["written"], True)
        self.assertTrue(target.is_file())

    def test_no_provider_usage(self):
        target = self.root / "routes.yaml"
        code, out, err = self.run_mr(["--config", str(target), "setup", "--yes"])
        self.assertEqual(code, exitcodes.USAGE, err)
        self.assertFalse(target.exists())
        self.assertIn("cursor-agent", err)
        self.assertIn(CURSOR_HINT, err)
        self.assertIn(CLAUDE_HINT, err)

    def test_provider_not_detected(self):
        self.exe("cursor-agent")
        target = self.root / "routes.yaml"
        code, out, err = self.run_mr(["--config", str(target), "setup", "--yes", "--provider", "claude"])
        self.assertEqual(code, exitcodes.USAGE, err)
        self.assertIn("not detected", err)
        self.assertIn("claude", err)
        self.assertIn(CLAUDE_HINT, err)
        self.assertFalse(target.exists())

    def test_override_executable(self):
        binary = self.root / "elsewhere" / "agent"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        target = self.root / "routes.yaml"
        code, out, err = self.run_mr(["--config", str(target), "setup", "--yes", "--provider", "cursor", "--cursor-agent", str(binary)])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertEqual(load(str(target)).paths.cursor_agent, str(binary))

    def test_override_not_executable(self):
        plain = self.root / "agent"
        plain.write_text("x", encoding="utf-8")
        target = self.root / "routes.yaml"
        code, out, err = self.run_mr(["--config", str(target), "setup", "--yes", "--provider", "cursor", "--cursor-agent", str(plain)])
        self.assertEqual(code, exitcodes.USAGE, err)
        self.assertIn("not detected", err)
        self.assertFalse(target.exists())

    def test_tty_decline_all(self):
        self.exe("cursor-agent")
        self.exe("claude")
        target = self.root / "routes.yaml"
        prompts = []

        def ask(prompt=""):
            prompts.append(prompt)
            return "n"

        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", ask):
            code, out, err = self.run_mr(["--config", str(target), "setup"])
        self.assertEqual(len(prompts), 2)
        self.assertIn("cursor", prompts[0])
        self.assertIn("claude", prompts[1])
        self.assertEqual(code, exitcodes.USAGE, err)
        self.assertIn(CURSOR_HINT, err)
        self.assertIn(CLAUDE_HINT, err)
        self.assertFalse(target.exists())

    def test_tty_yes_then_no(self):
        cursor = self.exe("cursor-agent")
        self.exe("claude")
        target = self.root / "routes.yaml"
        answers = iter(["y", "no"])

        def ask(prompt=""):
            return next(answers)

        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", ask):
            code, out, err = self.run_mr(["--config", str(target), "setup"])
        self.assertEqual(code, exitcodes.OK, err)
        cfg = load(str(target))
        self.assertEqual(cfg.paths.cursor_agent, str(cursor))
        self.assertEqual(cfg.advisor["mode"], "off")
        for rung in CLAUDE_RUNGS:
            self.assertNotIn(rung, self.ladder_codes(cfg))

    def test_tty_asks_only_detected(self):
        cursor = self.exe("cursor-agent")
        target = self.root / "routes.yaml"
        prompts = []

        def ask(prompt=""):
            prompts.append(prompt)
            return "y"

        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", ask):
            code, out, err = self.run_mr(["--config", str(target), "setup"])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertEqual(len(prompts), 1)
        self.assertIn("cursor", prompts[0])
        self.assertNotIn("claude", prompts[0])
        cfg = load(str(target))
        self.assertEqual(cfg.paths.cursor_agent, str(cursor))
        self.assertEqual(cfg.advisor["mode"], "off")

    def test_yes_skips_prompt_on_tty(self):
        self.exe("cursor-agent")
        self.exe("claude")
        target = self.root / "routes.yaml"

        def ask(*_a, **_k):
            raise AssertionError("prompted despite --yes")

        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", ask):
            code, out, err = self.run_mr(["--config", str(target), "setup", "--yes"])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertTrue(target.is_file())

    def test_check_reports_missing_binary(self):
        target = self.root / "routes.yaml"
        target.write_text(
            "version: 1\npaths:\n  claude: /no/such/claude\n  cursor_agent: /no/such/cursor-agent\n",
            encoding="utf-8",
        )
        before = target.read_text(encoding="utf-8")

        def ask(*_a, **_k):
            raise AssertionError("check prompted")

        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", ask):
            code, out, err = self.run_mr(["--config", str(target), "setup", "--check"])
        self.assertEqual(code, exitcodes.NOT_ACCEPTED, out + err)
        self.assertIn("cursor: missing", out)
        self.assertIn("claude: missing", out)
        self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_check_found_and_bad_config(self):
        cursor = self.exe("cursor-agent")
        claude = self.exe("claude")
        target = self.root / "routes.yaml"
        target.write_text(
            "version: 1\npaths:\n  claude: " + str(claude) + "\n  cursor_agent: " + str(cursor) + "\n",
            encoding="utf-8",
        )
        code, out, err = self.run_mr(["--config", str(target), "setup", "--check"])
        self.assertEqual(code, exitcodes.OK, out + err)
        self.assertIn("cursor: found", out)
        self.assertIn("claude: found", out)
        bare = self.root / "bare.yaml"
        bare.write_text("version: 1\n", encoding="utf-8")
        code, out, err = self.run_mr(["--config", str(bare), "setup", "--check"])
        self.assertEqual(code, exitcodes.OK, out + err)
        self.assertIn("cursor: found", out)
        self.assertIn("claude: found", out)
        bad = self.root / "bad.yaml"
        bad.write_text("version: 9\n", encoding="utf-8")
        code, out, err = self.run_mr(["--config", str(bad), "setup", "--check"])
        self.assertEqual(code, exitcodes.NOT_ACCEPTED)
        lines = [line for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        self.assertIn("version", out)

    def test_check_cursor_only_claude_not_used(self):
        self.exe("cursor-agent")
        target = self.root / "routes.yaml"
        code, out, err = self.run_mr(["--config", str(target), "setup", "--yes"])
        self.assertEqual(code, exitcodes.OK, err)
        code, out, err = self.run_mr(["--config", str(target), "setup", "--check"])
        self.assertEqual(code, exitcodes.OK, out + err)
        self.assertIn("cursor: found", out)
        self.assertIn("claude: not used", out)
        self.assertNotIn("claude: missing", out)

    def test_check_claude_only_skips_denied_fallbacks_without_check_script(self):
        self.exe("claude")
        target = self.root / "routes.yaml"
        code, out, err = self.run_mr(["--config", str(target), "setup", "--yes"])
        self.assertEqual(code, exitcodes.OK, err)
        code, out, err = self.run_mr(["--config", str(target), "setup", "--check"])
        self.assertEqual(code, exitcodes.OK, out + err)
        self.assertIn("cursor: not used", out)
        check = self.root / "check.sh"
        check.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        check.chmod(0o755)
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        raw["paths"]["check"] = str(check)
        target.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        code, out, err = self.run_mr(["--config", str(target), "setup", "--check"])
        self.assertEqual(code, exitcodes.NOT_ACCEPTED, out + err)
        self.assertIn("cursor: missing", out)

    def test_check_defaults_missing_claude(self):
        self.exe("cursor-agent")
        code, out, err = self.run_mr(["setup", "--check"])
        self.assertEqual(code, exitcodes.NOT_ACCEPTED, out + err)
        self.assertIn("claude: missing", out)
        self.assertIn("cursor: found", out)

    def test_default_config_path_and_mr_config(self):
        cursor = self.exe("cursor-agent")
        code, out, err = self.run_mr(["setup", "--yes", "--provider", "cursor"])
        self.assertEqual(code, exitcodes.OK, err)
        default = Path(os.environ["XDG_CONFIG_HOME"]) / "mr" / "routes.yaml"
        self.assertTrue(default.is_file())
        self.assertEqual(load(str(default)).paths.cursor_agent, str(cursor))
        other = self.root / "via-env.yaml"
        os.environ["MR_CONFIG"] = str(other)
        code, out, err = self.run_mr(["setup", "--yes", "--provider", "cursor"])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertTrue(other.is_file())
        pinned = self.root / "pinned.yaml"
        code, out, err = self.run_mr(["--config", str(pinned), "setup", "--yes", "--provider", "cursor"])
        self.assertEqual(code, exitcodes.OK, err)
        self.assertTrue(pinned.is_file())
        self.assertIn(str(pinned), out)


if __name__ == "__main__":
    unittest.main()
