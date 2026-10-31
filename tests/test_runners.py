"""Tests for mr.runners (mr-spec §5.7, §7.3, §10.4: AC-RUN-1..4, AC-MRR-1)."""

import json
import os
import stat
import tempfile
import time
import unittest
from unittest import mock

from mr import config, runners
from mr.defaults import defaults
from tests import gitfix

CHECK_DEFAULT = {
    "hour_usage": 10, "limit": 40, "unit": "points", "exceeded": False,
    "five_hour_pct": 30.0, "five_hour_resets_at": "2026-10-30T05:00:00Z",
    "seven_day_pct": 12.0, "sample_age_s": 30, "samples_in_hour": 3,
    "has_baseline": True, "checked_at": "2026-10-30T00:00:00Z",
}
SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"


def _dead(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] == "Z"
    except OSError:
        return True


class RunnerCase(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = td.name
        self.home = os.path.join(self.dir, "home")
        self.repo = gitfix.make_repo(os.path.join(self.dir, "repo"))
        self.cursor = gitfix.FAKE_CURSOR

    def setup(self, scenario, **cfg_paths):
        sp = gitfix.write_scenario(self.dir, scenario)
        cp = gitfix.write_check(self.dir, {"default": dict(CHECK_DEFAULT)})
        self.sp = sp
        patcher = mock.patch.dict(os.environ, gitfix.fake_env(sp, cp))
        patcher.start()
        self.addCleanup(patcher.stop)
        paths = {"home": self.home, "cursor_agent": self.cursor,
                 "claude": gitfix.FAKE_CLAUDE, "check": gitfix.FAKE_CHECK}
        paths.update(cfg_paths)
        self.cfg = config.from_dict(config._deep_merge(defaults(), {"paths": paths}))

    def calls(self):
        with open(self.sp + ".calls.jsonl") as fh:
            return [json.loads(line) for line in fh]

    def cursor_run(self, model="grok-4.7-high", timeout_s=30, mode=None):
        return runners.run_cursor(model, "do it", self.repo, mode, timeout_s,
                                  cfg=self.cfg, task="t1", attempt=1)

    def claude_run(self, rung="S", timeout_s=30):
        return runners.run_claude(rung, "do it", self.repo, "acceptEdits", None, timeout_s,
                                  cfg=self.cfg, task="t1", attempt=1)


class TestArgv(RunnerCase):
    def test_claude_argv_golden(self):
        self.setup({"claude": {SONNET: [{}], OPUS: [{}]}})
        for rung, model, effort in (("S", SONNET, "high"), ("O+", OPUS, "xhigh")):
            with self.subTest(rung=rung):
                guarded, res = self.claude_run(rung)
                self.assertIsNotNone(res)
                self.assertFalse(res.is_error, res.stderr_tail)
                argv = [gitfix.FAKE_CLAUDE] + self.calls()[-1]["argv"]
                golden = [
                    gitfix.FAKE_CLAUDE, "-p", "do it", "--output-format", "json",
                    "--model", model, "--effort", effort, "--permission-mode", "acceptEdits",
                    "--allowedTools",
                    "Read Grep Glob Edit Write Bash(npm run *) Bash(npx tsc*) "
                    "Bash(npx vitest*) Bash(git diff*) Bash(git status*)",
                    "--disallowedTools",
                    "Bash(git worktree *) Bash(git checkout *) Bash(git switch *) "
                    "Bash(git branch *) Bash(git reset *) Bash(git stash *) "
                    "Bash(git commit *) Bash(git push *)",
                    "--no-session-persistence",
                ]
                self.assertEqual(argv, golden)
                self.assertNotIn("--bare", argv)
                self.assertNotIn("--worktree", argv)

    def test_default_paths_prefix_and_schema(self):
        cfg = config.load()
        tail = runners.claude_argv_tail(cfg, "S", "p", "plan", '{"a":1}')
        argv = [cfg.paths.claude, *tail]
        self.assertEqual(argv[0], "claude")
        self.assertNotIn(cfg.paths.claude, tail)
        self.assertEqual(argv[-2:], ["--json-schema", '{"a":1}'])
        for bad in ("--bare", "--worktree"):
            self.assertNotIn(bad, argv)

    def test_cursor_argv(self):
        cfg = config.load()
        self.assertEqual(
            runners.cursor_argv(cfg, "m", "pp", "/t", "ask"),
            ["cursor-agent", "-p", "--output-format", "json", "--model", "m", "--trust",
             "--force", "--workspace", "/t", "--mode", "ask", "pp"])
        self.assertNotIn("--mode", runners.cursor_argv(cfg, "m", "pp", "/t", None))


class TestCursor(RunnerCase):
    def test_ok_envelope(self):
        self.setup({"cursor": {"grok-4.7-high": [{"files": {"a.txt": "x"}}]}})
        res = self.cursor_run()
        self.assertFalse(res.is_error)
        self.assertTrue(res.has_envelope)
        self.assertEqual(res.model_used, "grok-4.7-high")
        self.assertEqual(res.mr_result["status"], "done")
        self.assertIn("inputTokens", res.usage)
        self.assertTrue(res.session_id)
        self.assertTrue(os.path.exists(os.path.join(self.repo, "a.txt")))

    def test_cursor_error(self):
        self.setup({"cursor": {
            "m-err": [{"rc": 1, "is_error": True, "stderr": "boom happened"}],
            "m-raw": [{"no_envelope": True}]}})
        res = self.cursor_run("m-err")
        self.assertEqual(res.rc, 1)
        self.assertTrue(res.is_error)
        self.assertIn("boom happened", res.stderr_tail)
        res = self.cursor_run("m-raw")
        self.assertTrue(res.is_error)
        self.assertFalse(res.has_envelope)
        self.assertEqual(res.rc, 0)

    def test_stream_json_last_result_wins(self):
        lines = [json.dumps({"type": "system", "subtype": "init", "model": "served-x"}),
                 json.dumps({"type": "result", "is_error": False, "result": "old"}),
                 "junk", json.dumps({"type": "result", "is_error": False, "result": "new"})]
        out = "\n".join(lines)
        self.assertEqual(runners._envelope(out)["result"], "new")
        self.assertEqual(runners._init_model(out), "served-x")


class TestEnvAndCapture(RunnerCase):
    def test_child_env(self):
        self.setup({"cursor": {"grok-4.7-high": [{}]}, "claude": {SONNET: [{}]}})
        with mock.patch.dict(os.environ, {"CLOUDFLARE_API_TOKEN": "secret1",
                                          "CLOUDFLARE_ACCOUNT_ID": "secret2",
                                          "OPENROUTER_API_KEY": "secret3",
                                          "CLAUDE_CODE_DISABLE_ADVISOR_TOOL": ""}):
            os.environ.pop("CLAUDE_CODE_DISABLE_ADVISOR_TOOL")
            self.cursor_run()
            self.claude_run()
        cur, cla = self.calls()
        for rec in (cur, cla):
            keys = rec["env"]["env_keys"]
            self.assertFalse([k for k in keys if k.startswith("CLOUDFLARE_")])
            self.assertNotIn("OPENROUTER_API_KEY", keys)
            self.assertEqual(rec["env"]["GIT_CONFIG_COUNT"], "3")
            for i, val in enumerate(("https://", "git@", "ssh://")):
                self.assertEqual(rec["env"][f"GIT_CONFIG_KEY_{i}"],
                                 "url.file:///nonexistent/.pushInsteadOf")
                self.assertEqual(rec["env"][f"GIT_CONFIG_VALUE_{i}"], val)
        self.assertNotIn("CLAUDE_CODE_DISABLE_ADVISOR_TOOL", cur["env"])
        self.assertEqual(cla["env"]["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"], "1")

    def test_raw_capture(self):
        self.setup({"cursor": {"m-e": [{"stderr": "some err"}]}})
        res = self.cursor_run("m-e")
        self.assertEqual(res.raw_path, f"{self.home}/raw/t1/a1-m-e")
        with open(res.raw_path + ".out") as fh:
            self.assertEqual(json.loads(fh.read())["type"], "result")
        with open(res.raw_path + ".err") as fh:
            self.assertIn("some err", fh.read())

    def test_stderr_cap(self):
        base = os.path.join(self.dir, "raw", "x")
        script = os.path.join(self.dir, "big.sh")
        with open(script, "w") as fh:
            fh.write("#!/bin/sh\nhead -c 200000 /dev/zero | tr '\\0' e >&2\n")
        os.chmod(script, 0o755)
        rc, _, err, _, _ = runners.exec_child([script], self.dir, dict(os.environ), 30, base)
        self.assertEqual((rc, len(err)), (0, 64 * 1024))
        self.assertEqual(os.path.getsize(base + ".err"), 64 * 1024)


class TestTimeout(RunnerCase):
    def test_timeout_killpg(self):
        self.setup({"cursor": {"m-slow": [{"sleep_s": 100000}]}})  # x0.01 => 1000 s
        t0 = time.monotonic()
        res = self.cursor_run("m-slow", timeout_s=100)  # x0.01 => 1 s
        self.assertLess(time.monotonic() - t0, 20)
        self.assertTrue(res.timed_out)
        self.assertTrue(res.is_error)
        self.assertNotEqual(res.rc, 0)
        self.assertFalse(runners._children)

    def test_timeout_kills_grandchild(self):
        pidfile = os.path.join(self.dir, "gc.pid")
        script = os.path.join(self.dir, "spawner.sh")
        with open(script, "w") as fh:
            fh.write(f"#!/bin/sh\nsleep 30 &\necho $! > {pidfile}\nsleep 30\n")
        os.chmod(script, os.stat(script).st_mode | stat.S_IXUSR)
        self.setup({}, cursor_agent=script)
        res = self.cursor_run(timeout_s=100)
        self.assertTrue(res.timed_out)
        with open(pidfile) as fh:
            pid = int(fh.read())
        deadline = time.monotonic() + 5
        while not _dead(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(_dead(pid), "grandchild survived")


class TestCursorRefusesAnthropic(RunnerCase):
    def test_cursor_refuses_anthropic(self):  # AC-RUN-5, hard rule §0.3
        self.setup({"cursor": {"grok-4.7-high": [{}]}})
        msg = "Anthropic models run only via the claude CLI (hard rule)"
        with mock.patch.object(runners, "exec_child") as ex:
            ex.side_effect = AssertionError("exec_child must not be called")
            for model in ("claude-sonnet-5-5-high", "sonnet", "OPUS", "claude-haiku-5-5"):
                with self.subTest(model=model):
                    with self.assertRaises(config.ConfigError) as caught:
                        self.cursor_run(model)
                    self.assertEqual(str(caught.exception), msg)
                    with self.assertRaises(config.ConfigError) as caught:
                        runners.cursor_argv(self.cfg, model, "p", self.repo, None)
                    self.assertEqual(str(caught.exception), msg)
            ex.assert_not_called()
        res = self.cursor_run("grok-4.7-high")  # non-Anthropic still runs
        self.assertFalse(res.is_error)


class TestParseMrResult(unittest.TestCase):
    def test_parse_mr_result(self):
        p = runners.parse_mr_result
        self.assertEqual(p('x\nMR_RESULT {"status":"done"}\n'), {"status": "done"})
        self.assertIsNone(p("no marker here"))
        self.assertEqual(p('MR_RESULT {"a":1}\nMR_RESULT {"a":2}'), {"a": 2})
        self.assertIsNone(p('MR_RESULT {"a":1}\nMR_RESULT {bad'))  # last wins, invalid
        self.assertIsNone(p("MR_RESULT [1,2]"))
        self.assertIsNone(p("MR_RESULT null"))
        self.assertIsNone(p('  MR_RESULT {"a":1}'))  # must start the line
        self.assertIsNone(p('MR_RESULT{"a":1}'))  # needs the space
        old = 'MR_RESULT {"a":1}\n' + "\n".join(f"l{i}" for i in range(50))
        self.assertIsNone(p(old))
        edge = 'MR_RESULT {"a":1}\n' + "\n".join(f"l{i}" for i in range(49))
        self.assertEqual(p(edge), {"a": 1})
        self.assertEqual(p('MR_REVIEW {"v":1}', tag="MR_REVIEW"), {"v": 1})
        self.assertIsNone(p('MR_REVIEW {"v":1}'))


class TestClaudeRun(RunnerCase):
    def test_result_fields(self):
        self.setup({"claude": {SONNET: [{"cost": 0.12}]}})
        guarded, res = self.claude_run()
        self.assertTrue(guarded.guard.allow)
        self.assertFalse(guarded.wrapper_denied)
        self.assertFalse(res.is_error)
        self.assertEqual((res.usd_equiv, res.turns, res.model_used), (0.12, 3, SONNET))
        self.assertFalse(res.model_drift)
        self.assertTrue(res.session_id.startswith("fake-claude-"))
        self.assertEqual(res.mr_result["status"], "done")
        self.assertIn("input_tokens", res.usage)

    def test_wrapper_75(self):
        self.setup({"claude": {SONNET: [{"exit": 75}]}})
        guarded, res = self.claude_run()
        self.assertEqual(res.rc, 75)
        self.assertTrue(res.is_error)
        self.assertTrue(guarded.wrapper_denied)
        self.assertIn("hourly limit", res.stderr_tail)

    def test_model_drift(self):
        self.setup({"claude": {OPUS: [{"model_served": "claude-opus-5-5-thinking"}]}})
        _, res = self.claude_run("O+")
        self.assertTrue(res.model_drift)
        self.assertEqual(res.model_used, "claude-opus-5-5-thinking")

    def test_denied_before_running(self):
        self.setup({"claude": {SONNET: [{}]}})
        gitfix.write_check(self.dir, {"default": dict(CHECK_DEFAULT, exceeded=True)})
        guarded, res = self.claude_run()
        self.assertIsNone(res)
        self.assertFalse(guarded.guard.allow)
        self.assertFalse(os.path.exists(self.sp + ".calls.jsonl"))


if __name__ == "__main__":
    unittest.main()
